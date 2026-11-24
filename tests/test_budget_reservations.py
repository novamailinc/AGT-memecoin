import json,os,sqlite3,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import pytest
from daemon.intelligence.acquisition import provider_usage as usage
from daemon.intelligence.acquisition.budget_reservations import reserve
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.acquisition.provider_router import ProviderRouter,ROUTING_MODE_REVISED
from daemon.intelligence.acquisition.helius_parsed_events import HeliusParsedEventsClient


@pytest.mark.parametrize('cost',[None,float('nan'),float('inf'),-1,True,'1'])
def test_unknown_invalid_metered_cost_refuses(tmp_path,cost):
    assert not reserve(UsageTracker(home=str(tmp_path)),'helius_main','helius','routine_account',cost)[0]


def test_projected_monthly_reservation_survives_new_instance(tmp_path,monkeypatch):
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'monthly_quota':{'amount':10,'unit':'credits'}})
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',6)[0]
    assert not reserve(UsageTracker(home=str(tmp_path)),'helius_main','helius','routine_account',5)[0]
    assert reserve(u,'helius_main','helius','routine_account',4)[0]
    assert not reserve(u,'helius_main','helius','routine_account',1)[0]

@pytest.mark.parametrize('scope',['daily','feature'])
def test_daily_and_feature_projected_limits(tmp_path,monkeypatch,scope):
    if scope=='daily':monkeypatch.setitem(usage.BUDGET_OVERRIDES,'helius_main',{'daily_amount':10})
    else:monkeypatch.setitem(usage.FEATURE_BUDGETS,('helius_main','standard_rpc'),{'amount':10})
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',7)[0]
    out=reserve(u,'helius_main','helius','routine_account',4)
    assert not out[0] and scope in out[1]


def test_actual_router_reserves_before_transport_and_unknown_failure_not_refunded(tmp_path,monkeypatch):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'enabled':True,'monthly_quota':{'amount':10,'unit':'credits'}} if account=='helius_main' else None)
    seen=[]
    def transport(req,timeout=None):
        with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:seen.append(db.execute('SELECT sum(cost) FROM reservations').fetchone()[0])
        raise TimeoutError('fixture uncertainty')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        r.call('getTransaction',['fixture'],max_attempts=1)
    assert seen==[10]
    assert not reserve(UsageTracker(home=str(tmp_path)),'helius_main','helius','routine_tx',1)[0]


def test_actual_selective_rest_unknown_failure_retains_projected_cost(tmp_path,monkeypatch):
    calls=[]
    def transport(*a,**kw):
        with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:calls.append(db.execute('SELECT sum(cost) FROM reservations').fetchone()[0])
        raise TimeoutError('fixture uncertainty')
    c=HeliusParsedEventsClient(api_key='fixture',_opener=transport);c.account_home=str(tmp_path)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='timeout'
    assert calls==[10]
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==1


def test_competing_fresh_processes_cannot_reserve_past_limit(tmp_path):
    code='''import sys
from daemon.intelligence.acquisition import provider_usage as u
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.acquisition.budget_reservations import reserve
u.caps.get_account=lambda account:{'monthly_quota':{'amount':10,'unit':'credits'}}
print(int(reserve(UsageTracker(home=sys.argv[1]),'helius_main','helius','routine_account',6)[0]))
'''
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]))
    procs=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(6)]
    results=[p.communicate(timeout=10) for p in procs]
    assert all(p.returncode==0 for p in procs),results
    assert sum(int(out.strip()) for out,err in results)==1
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:assert db.execute('SELECT sum(cost) FROM reservations').fetchone()[0]==6


def test_lock_or_corruption_refuses_before_io(tmp_path):
    path=tmp_path/'acquisition_budget_reservations.sqlite3';path.write_bytes(b'broken')
    out=reserve(UsageTracker(home=str(tmp_path)),'helius_main','helius','routine_account',1)
    assert not out[0] and out[1]=='budget_reservation_unavailable'
    assert path.read_bytes()==b'broken'


def test_completed_usage_is_conservatively_double_counted_not_released(tmp_path,monkeypatch):
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'monthly_quota':{'amount':10,'unit':'credits'}})
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',4)[0]
    u.record('helius_main','helius','getSlot','routine_account','card',cost=4)
    assert not reserve(u,'helius_main','helius','routine_account',3)[0]
    assert reserve(u,'helius_main','helius','routine_account',2)[0]


def test_abrupt_child_after_reservation_keeps_debit_on_fresh_process(tmp_path,monkeypatch):
    code="""import sys,os
from daemon.intelligence.acquisition import provider_usage as u
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.acquisition.budget_reservations import reserve
u.caps.get_account=lambda account:{'monthly_quota':{'amount':10,'unit':'credits'}}
assert reserve(UsageTracker(home=sys.argv[1]),'helius_main','helius','routine_account',10)[0]
os._exit(47)
"""
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]))
    assert subprocess.run([sys.executable,'-c',code,str(tmp_path)],env=env,timeout=10).returncode==47
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:assert db.execute('SELECT sum(cost) FROM reservations').fetchone()[0]==10
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'monthly_quota':{'amount':10,'unit':'credits'}})
    assert reserve(UsageTracker(home=str(tmp_path)),'helius_main','helius','routine_account',1)[0] is False


def test_invalid_retained_reservation_refused(tmp_path):
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',1)[0]
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:db.execute('UPDATE reservations SET cost=-100')
    assert reserve(u,'helius_main','helius','routine_account',1)==(False,'budget_reservation_unavailable',None)


def test_commit_directory_durability_failure_refuses_but_keeps_reservation(tmp_path):
    u=UsageTracker(home=str(tmp_path))
    with patch('os.fsync',side_effect=OSError('fixture dir sync')):
        assert reserve(u,'helius_main','helius','routine_account',10)==(False,'budget_reservation_unavailable',None)
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:assert db.execute('SELECT sum(cost) FROM reservations').fetchone()[0]==10


def test_locked_reservation_db_refuses_without_erasing_hold(tmp_path):
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',1)[0]
    db=sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3');db.execute('BEGIN IMMEDIATE')
    try:assert reserve(u,'helius_main','helius','routine_account',1)==(False,'budget_reservation_unavailable',None)
    finally:db.rollback();db.close()
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:assert db.execute('SELECT sum(cost) FROM reservations').fetchone()[0]==1


def test_selective_rest_projected_exhaustion_no_transport(tmp_path,monkeypatch):
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'enabled':True,'monthly_quota':{'amount':9,'unit':'credits'}})
    calls=[]
    c=HeliusParsedEventsClient(api_key='fixture',_opener=lambda *a,**kw:calls.append(a));c.account_home=str(tmp_path)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'
    assert not calls


def test_actual_router_unknown_metered_cost_never_calls_transport(tmp_path,monkeypatch):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    calls=[];r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=lambda *a,**kw:calls.append(a))
    monkeypatch.setattr(r._adapter('helius'),'cost',lambda method:None)
    result=r.call('getSlot',[],max_attempts=1)
    assert not calls and result['kind']=='rate_limited'
    assert result['attempts'][0]['note']=='unknown_projected_cost'


@pytest.mark.parametrize('field,value',[
 ('month','broken'),('day','broken'),('month','2001-01'),('day','2001-01-01'),
 ('account',''),('account','   '),('account',7),('feature',''),('feature','   '),('feature',7),
 ('id',''),('id','bad'),('created',0),('created',-1),('created','not-time'),('cost','not-cost')])
def test_malformed_retained_identity_cannot_hide_holds(tmp_path,monkeypatch,field,value):
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'monthly_quota':{'amount':10,'unit':'credits'}})
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',10)[0]
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:db.execute('UPDATE reservations SET '+field+'=?',(value,))
    assert reserve(u,'helius_main','helius','routine_account',10)==(False,'budget_reservation_unavailable',None)


def test_actual_router_malformed_retained_window_refuses_before_transport(tmp_path,monkeypatch):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    u=UsageTracker(home=str(tmp_path));assert reserve(u,'helius_main','helius','routine_account',1)[0]
    with sqlite3.connect(tmp_path/'acquisition_budget_reservations.sqlite3') as db:db.execute("UPDATE reservations SET month='bad',day='bad'")
    calls=[];r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=lambda *a,**kw:calls.append(a))
    result=r.call('getSlot',[],max_attempts=1)
    assert not calls and result['kind']=='rate_limited'
    assert result['attempts'][0]['note']=='budget_reservation_unavailable'


def test_unknown_outcome_no_key_hop_and_ledger_matches_attempts(tmp_path,monkeypatch):
    # Unknown post-I/O outcome on the home key: NO failover hop to
    # another key (an uncertain request is never retried), the credit
    # reservation taken before the attempt is retained (never refunded),
    # and retained reservations stay 1:1 with actual transport attempts.
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'enabled':True,'monthly_quota':{'amount':100,'unit':'credits'}})
    calls=[]
    def transport(req,timeout=None):
        calls.append(1)
        raise TimeoutError('fixture uncertainty')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        out=r.call('getBalance',['fixture'],max_attempts=3)
    assert out['kind']=='timeout'
    assert len(calls)==1  # one actual attempt despite 3 configured keys
    import sqlite3 as _s
    with _s.connect(str(tmp_path)+'/acquisition_budget_reservations.sqlite3') as db:
        rows=db.execute('SELECT count(*) FROM reservations').fetchone()[0]
    assert rows==len(calls)  # one retained reservation per actual attempt


def test_exhausted_credit_budget_makes_no_transport_attempt(tmp_path,monkeypatch):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'enabled':True,'monthly_quota':{'amount':0,'unit':'credits'}} if account=='helius_main' else None)
    calls=[]
    def transport(req,timeout=None):
        calls.append(1);raise AssertionError('transport must not run')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        r.call('getTransaction',['fixture'],max_attempts=1)
    assert calls==[]


def test_unknown_outcome_blocks_cross_provider_failover(tmp_path,monkeypatch):
    # Unknown post-I/O outcome on Helius: the router must NOT move to
    # the next PROVIDER either, even with max_attempts>1. An uncertain
    # request is never retried anywhere.
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-alchemy')
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'enabled':True,'monthly_quota':{'amount':100,'unit':'credits'}})
    calls=[]
    def transport(req,timeout=None):
        calls.append(1)
        raise TimeoutError('fixture uncertainty')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        out=r.call('getBalance',['fixture'],max_attempts=3)
    assert out['kind']=='timeout'
    assert len(calls)==1  # no second key, no second provider
    attempted=[a for a in out['attempts'] if a['kind']!='skipped']
    assert len(attempted)==1 and attempted[0]['provider']=='helius'


def test_answered_rpc_error_still_fails_over(tmp_path,monkeypatch):
    # An ANSWERED outcome (JSON-RPC error body: the vendor definitively
    # processed and refused the request) is not uncertain; failover
    # semantics for answered outcomes are unchanged.
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    monkeypatch.setattr(usage.caps,'get_account',lambda account:{'enabled':True,'monthly_quota':{'amount':100,'unit':'credits'}})
    def transport(req,timeout=None):
        return type('R',(),{'read':staticmethod(lambda:b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"invalid params"}}'),'status':200,'headers':{},'__enter__':lambda s:s,'__exit__':lambda s,*a:False})()
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    with patch('daemon.launch_scanner.helius_ratelimit.pacer') as p:
        p.return_value.acquire.return_value=True
        out=r.call('getBalance',['fixture'],max_attempts=3)
    # The answered outcome was NOT treated as uncertain: no unknown
    # marker, no no-failover note. (An answered RPC-level refusal is
    # terminal in the router's pre-existing semantics - every provider
    # would answer identically - so no failover is expected here; the
    # property under test is that it is not misclassified as unknown.)
    assert out.get('outcome_unknown') is not True
    assert all('no failover' not in (a.get('note') or '') for a in out['attempts'])


def test_pre_io_attempts_counted_zero(tmp_path,monkeypatch):
    # transport_attempts precision: pre-I/O outcomes (unconfigured,
    # unsupported) consume zero attempt budget; a real send consumes
    # exactly one. The router's remaining-budget handoff depends on it.
    from daemon.intelligence.acquisition.providers.alchemy import AlchemyAdapter
    monkeypatch.delenv('ALCHEMY_API_KEY',raising=False)
    a=AlchemyAdapter(home=str(tmp_path))
    a._transport=lambda req,timeout=None:(_ for _ in ()).throw(AssertionError('must not be called'))
    out=a.call('getBalance',['x'])
    assert out['kind']=='unconfigured' and out['transport_attempts']==0
    a2=AlchemyAdapter(home=str(tmp_path))
    monkeypatch.setattr('daemon.intelligence.acquisition.providers.base._alchemy_credential_key',lambda home=None:'fixture')
    a2._transport=lambda req,timeout=None:(_ for _ in ()).throw(TimeoutError('fixture'))
    out2=a2.call('getBalance',['x'])
    assert out2['kind']=='timeout' and out2['transport_attempts']==1 and out2['outcome_unknown'] is True


def test_reserves_typed_no_hop_on_unknown(monkeypatch):
    # v4 contract: reserves.helius_rpc_typed never retries an uncertain
    # request on the next URL/bucket pair. fail = unknown post-I/O.
    from daemon.launch_scanner import reserves as R
    pairs=[('https://mainnet.helius-rpc.com/?api-key=k1','b1'),
           ('https://mainnet.helius-rpc.com/?api-key=k2','b2')]
    monkeypatch.setattr(R,'rpc_url_pairs',lambda:pairs)
    class P:
        def acquire(self,b):return True
        def report(self,b,ok):pass
    monkeypatch.setattr(R,'_pacer',lambda:P())
    calls=[]
    def attempt(url,body,method,host,timeout):
        calls.append(url)
        return None,'fail',0.001
    monkeypatch.setattr(R,'_rpc_attempt',attempt)
    out,kind,attempts=R.helius_rpc_typed('getBalance',[['x']])
    assert (out,kind,attempts)==(None,'fail',1) and len(calls)==1


def test_reserves_typed_429_still_hops(monkeypatch):
    # Answered 429s remain eligible for the next pair (not uncertain).
    from daemon.launch_scanner import reserves as R
    pairs=[('https://mainnet.helius-rpc.com/?api-key=k1','b1'),
           ('https://mainnet.helius-rpc.com/?api-key=k2','b2')]
    monkeypatch.setattr(R,'rpc_url_pairs',lambda:pairs)
    class P:
        def acquire(self,b):return True
        def report(self,b,ok):pass
    monkeypatch.setattr(R,'_pacer',lambda:P())
    calls=[]
    def attempt(url,body,method,host,timeout):
        calls.append(url)
        return None,'429',0.001
    monkeypatch.setattr(R,'_rpc_attempt',attempt)
    out,kind,attempts=R.helius_rpc_typed('getBalance',[['x']])
    assert (out,kind,attempts)==(None,'error',2) and len(calls)==2


def test_legacy_helius_adapter_unknown_marked(tmp_path,monkeypatch):
    # Legacy (non-revised) path: reserves 'fail' and exceptions both
    # surface as outcome_unknown with attempt accounting, so the v3
    # router no-failover break covers every reachable mode.
    from daemon.intelligence.acquisition.providers.helius import HeliusAdapter
    from daemon.launch_scanner import reserves
    a=HeliusAdapter(home=str(tmp_path))
    monkeypatch.setattr(reserves,'helius_rpc_typed',
                        lambda m,p,timeout=12,**kw:(None,'fail',1))
    out=a.call('getBalance',['x'])
    assert out['kind']=='error' and out['outcome_unknown'] is True and out['transport_attempts']==1
    def boom(m,p,timeout=12,**kw):raise RuntimeError('post-invoke')
    monkeypatch.setattr(reserves,'helius_rpc_typed',boom)
    out=a.call('getBalance',['x'])
    assert out['kind']=='error' and out['outcome_unknown'] is True and out['transport_attempts']==1
    monkeypatch.setattr(reserves,'helius_rpc_typed',
                        lambda m,p,timeout=12,**kw:(None,'error',0))
    out=a.call('getBalance',['x'])
    assert out['kind']=='error' and out.get('outcome_unknown') is not True and out['transport_attempts']==0


def test_revised_shared_hold_attempts_zero(tmp_path,monkeypatch):
    # Shared-account hold is a pre-I/O refusal: zero attempts consumed.
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    from daemon.intelligence.acquisition.providers import helius as H
    from daemon.intelligence.acquisition.providers.helius import HeliusAdapter
    from daemon.intelligence.acquisition import helius_account
    monkeypatch.setattr(helius_account,'allowed',lambda home=None:False)
    a=HeliusAdapter(home=str(tmp_path)); a.revised=True
    out=H.revised_call(a,'getBalance',['x'],12)
    assert out['kind']=='rate_limited' and out['transport_attempts']==0


def test_reserves_typed_429_hop_respects_max_attempts(monkeypatch):
    # Answered-429 eligibility is NOT an unlimited attempt budget: the
    # caller's per-call cap bounds TOTAL sends across the legacy loop.
    from daemon.launch_scanner import reserves as R
    pairs=[('https://mainnet.helius-rpc.com/?api-key=k1','b1'),
           ('https://mainnet.helius-rpc.com/?api-key=k2','b2'),
           ('https://mainnet.helius-rpc.com/?api-key=k3','b3')]
    monkeypatch.setattr(R,'rpc_url_pairs',lambda:pairs)
    class P:
        def acquire(self,b):return True
        def report(self,b,ok):pass
    monkeypatch.setattr(R,'_pacer',lambda:P())
    calls=[]
    def attempt(url,body,method,host,timeout):
        calls.append(url)
        return None,'429',0.001
    monkeypatch.setattr(R,'_rpc_attempt',attempt)
    out,kind,attempts=R.helius_rpc_typed('getBalance',[['x']],max_attempts=1)
    assert (out,kind,attempts)==(None,'error',1) and len(calls)==1
    out,kind,attempts=R.helius_rpc_typed('getBalance',[['x']],max_attempts=2)
    assert (out,kind,attempts)==(None,'error',2) and len(calls)==3


def test_legacy_adapter_exception_charges_exact_sends(tmp_path,monkeypatch):
    # Exception after recorded sends: the adapter charges the exact
    # sends made (from attempts_out), not a flat guess.
    from daemon.intelligence.acquisition.providers.helius import HeliusAdapter
    from daemon.launch_scanner import reserves as R
    pairs=[('https://mainnet.helius-rpc.com/?api-key=k1','b1'),
           ('https://mainnet.helius-rpc.com/?api-key=k2','b2')]
    monkeypatch.setattr(R,'rpc_url_pairs',lambda:pairs)
    class P:
        def __init__(self):self.n=0
        def acquire(self,b):
            self.n+=1
            if self.n==2:raise RuntimeError('pacer blew up mid-loop')
            return True
        def report(self,b,ok):pass
    pacer=P()
    monkeypatch.setattr(R,'_pacer',lambda:pacer)
    monkeypatch.setattr(R,'_rpc_attempt',lambda *a,**k:(None,'429',0.001))
    a=HeliusAdapter(home=str(tmp_path))
    monkeypatch.setattr(R,'rpc_url_pairs',lambda:pairs)
    out=a.call('getBalance',['x'])
    assert out['kind']=='error' and out['outcome_unknown'] is True
    assert out['transport_attempts']==1  # exactly the send made before the raise
