import copy,json
from types import SimpleNamespace
import pytest
from daemon.launch_scanner import exit_copy as e


def record(ts=10,mint='mint'):
    return dict(mint=mint,entry_ts=1,close_ts=ts,close_mult=1.5,reason='tp',pnl_sol=.02,wallet_exits={},scanned_day='')


def test_exact_closes_same_mint_distinct_and_retry(tmp_path):
    p=tmp_path/'copy.json'
    assert e.append_close(p,record(),'a')[0]
    assert not e.append_close(p,record(),'a')[0]
    assert e.append_close(p,record(),'b')[0]
    assert len(e._all_closes(p))==2
    with pytest.raises(ValueError):e.append_close(p,record(11),'a')


def test_stale_enrichment_merges_without_losing_new_close(tmp_path):
    p=tmp_path/'copy.json';first=record();e.append_close(p,first,'a')
    stale=copy.deepcopy(e._all_closes(p)['a']);e.append_close(p,record(11),'b')
    exits={'wallet':{'sold_ts':5,'after_our_close_h':round(-5/3600,2)}}
    e.merge_enrichment(p,'a',stale,exits,'2026-11-23')
    assert set(e._all_closes(p))=={'a','b'}
    assert e._all_closes(p)['a']['wallet_exits']==exits
    e.merge_enrichment(p,'a',stale,exits,'2026-11-23')
    assert len(e._all_closes(p))==2
    with pytest.raises(ValueError):e.merge_enrichment(p,'a',dict(stale,pnl_sol=99),{},'2026-11-23')


def test_legacy_import_and_corrupt_refusal(tmp_path):
    p=tmp_path/'copy.json';legacy=record();legacy.pop('mint')
    p.write_text(json.dumps({'mint':legacy}));e.append_close(p,record(11),'new')
    assert set(e._all_closes(p))=={'mint','new'}
    p.write_text('{bad')
    with pytest.raises(ValueError):e.append_close(p,record(),'no')
    assert p.read_text()=='{bad'
    assert 'unavailable' in ' '.join(e.weekly_section(str(p)))


def test_old_replay_cannot_evict_latest(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';monkeypatch.setattr(e,'_MAX_CLOSES',2)
    for key,ts in [('a',1000000),('b',1000001),('old',10)]:e.append_close(p,record(ts),key)
    assert set(e._all_closes(p))=={'a','b'}
    assert not e.append_close(p,record(10),'old')[0]


def test_unavailable_never_reports_success(tmp_path):
    p=tmp_path/'copy.json';p.write_text('{bad')
    assert e.record_close(SimpleNamespace(), 'mint',1,2,1,'x',0,str(p)) is None
    assert p.read_text()=='{bad'


def test_failed_enrichment_does_not_mark_day_complete(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';now=1795470000.;e.append_close(p,record(now),'a')
    state={'smart_activity':{}}
    monkeypatch.setattr(e,'merge_enrichment',lambda *a:(_ for _ in ()).throw(OSError('disk')))
    assert e.daily_pass(state,SimpleNamespace(),now,str(p))==0
    assert 'exit_copy_last_pass_day' not in state


def test_merge_visible_fault_retry_and_concurrent_wallets(tmp_path,monkeypatch):
    from daemon.launch_scanner import learning_effects as effects
    p=tmp_path/'copy.json';source=record();e.append_close(p,source,'a')
    real=effects.os.replace
    def fault(a,b):real(a,b);raise OSError('visible replacement')
    patch={'wallet-a':{'sold_ts':5,'after_our_close_h':0.}}
    with monkeypatch.context() as m:
        m.setattr(effects.os,'replace',fault)
        with pytest.raises(OSError):e.merge_enrichment(p,'a',source,patch,'2026-11-23')
    e.merge_enrichment(p,'a',source,{'wallet-b':{'sold_ts':6,'after_our_close_h':0.}},'2026-11-23')
    e.merge_enrichment(p,'a',source,patch,'2026-11-23')
    assert set(e._all_closes(p)['a']['wallet_exits'])=={'wallet-a','wallet-b'}


@pytest.mark.parametrize('field,value',[('close_ts',float('nan')),('pnl_sol',float('inf')),('close_mult',-1),('entry_ts',11)])
def test_invalid_record_never_mutates_store(tmp_path,field,value):
    p=tmp_path/'copy.json';e.append_close(p,record(),'a');before=p.read_bytes()
    bad=record();bad[field]=value
    with pytest.raises(ValueError):e.append_close(p,bad,'bad')
    assert p.read_bytes()==before


def test_provider_failure_retries_without_resetting_durable_budget(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';now=1795470000.;e.append_close(p,record(now),'a')
    monkeypatch.setattr(e.time,'time',lambda:now)
    state={'smart_activity':{'mint':['wallet']}};calls=[]
    def fail(*args):calls.append(args);raise OSError('provider unavailable')
    monkeypatch.setattr(e,'_rpc',fail)
    cfg=SimpleNamespace(exit_copy_max_tx_per_day=2)
    for _ in range(4):
        assert e.daily_pass(state,cfg,now,str(p))==0
        assert 'exit_copy_last_pass_day' not in state
        assert e._all_closes(p)['a']['scanned_day']==''
    assert len(calls)==2
    # A new caller/state does not get another provider budget today.
    e.daily_pass({'smart_activity':{'mint':['wallet']}},cfg,now,str(p))
    assert len(calls)==2


def test_confirmed_empty_history_can_complete(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';now=1795470000.;e.append_close(p,record(now),'a')
    monkeypatch.setattr(e.time,'time',lambda:now);monkeypatch.setattr(e,'_rpc',lambda *a:[])
    state={'smart_activity':{'mint':['wallet']}}
    assert e.daily_pass(state,SimpleNamespace(),now,str(p))==1
    assert state['exit_copy_last_pass_day']=='2026-11-23'


@pytest.mark.parametrize('day,exits',[('not-a-day',{}),('2026-1-1',{}),('',{'w':{'sold_ts':20,'after_our_close_h':-1}}),('',{'w':{'sold_ts':0,'after_our_close_h':0}})])
def test_invalid_enrichment_date_or_timing_refused(tmp_path,day,exits):
    p=tmp_path/'copy.json';source=record();e.append_close(p,source,'a');before=p.read_bytes()
    with pytest.raises(ValueError):e.merge_enrichment(p,'a',source,exits,day)
    assert p.read_bytes()==before


def test_partial_sale_reinvestigated_then_earliest_improved(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';now=1795470000.;source=record(now);e.append_close(p,source,'a')
    monkeypatch.setattr(e.time,'time',lambda:now)
    state={'smart_activity':{'mint':['wallet']}};calls=[]
    def investigate(*a,**kw):
        calls.append(a)
        return {'sold_ts':now-10 if len(calls)==1 else now-20,'complete':len(calls)>1}
    monkeypatch.setattr(e,'_wallet_sold_after',investigate)
    assert e.daily_pass(state,SimpleNamespace(),now,str(p))==0
    assert e._all_closes(p)['a']['wallet_exits']['wallet']['complete'] is False
    assert e.daily_pass(state,SimpleNamespace(),now,str(p))==1
    assert len(calls)==2
    assert e._all_closes(p)['a']['wallet_exits']['wallet']['sold_ts']==now-20


def test_causal_partial_parse_budget_retries_earlier_sale(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';now=1795470000.;e.append_close(p,record(now),'a')
    monkeypatch.setattr(e.time,'time',lambda:now);monkeypatch.setattr(e.time,'sleep',lambda _:None)
    def rpc(method,params):
        if method=='getSignaturesForAddress':return [{'signature':'late','blockTime':now-10},{'signature':'early','blockTime':now-20}]
        return {'transaction':{'signatures':[params[0]]},'blockTime':now-10 if params[0]=='late' else now-20,
                'meta':{'err':None,'preTokenBalances':[{'accountIndex':1,'owner':'wallet','mint':'mint','uiTokenAmount':{'uiAmount':5,'amount':'5','decimals':0}}],
                        'postTokenBalances':[{'accountIndex':1,'owner':'wallet','mint':'mint','uiTokenAmount':{'uiAmount':1,'amount':'1','decimals':0}}]}}
    monkeypatch.setattr(e,'_rpc',rpc);real=e._wallet_sold_after;calls=[]
    def limited(wallet,mint,ts,budget,**kw):
        calls.append(1)
        return real(wallet,mint,ts,[1 if len(calls)==1 else 2],**kw)
    monkeypatch.setattr(e,'_wallet_sold_after',limited)
    state={'smart_activity':{'mint':['wallet']}};cfg=SimpleNamespace(exit_copy_max_tx_per_day=10)
    assert e.daily_pass(state,cfg,now,str(p))==0
    assert e._all_closes(p)['a']['wallet_exits']['wallet']['sold_ts']==now-10
    assert e.daily_pass(state,cfg,now,str(p))==1
    assert e._all_closes(p)['a']['wallet_exits']['wallet']=={'sold_ts':now-20,'after_our_close_h':round(-20/3600,2),'complete':True}


def test_cross_process_budget_cap(tmp_path):
    import os,subprocess,sys
    p=tmp_path/'copy.json'
    source='''
import sys
from daemon.launch_scanner.exit_copy import reserve_daily_call
for _ in range(3):
    try:reserve_daily_call(sys.argv[1],'2026-11-23',4)
    except ValueError:pass
'''
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    children=[subprocess.Popen([sys.executable,'-c',source,str(p)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3)]
    for child in children:
        _,err=child.communicate(timeout=20)
        assert child.returncode==0,err
    assert json.loads((tmp_path/'copy.json.budget.json').read_text())['value']=={'2026-11-23':4}


def test_durable_progress_advances_across_low_budget_days(tmp_path,monkeypatch):
    p=tmp_path/'copy.json';now=1795470000.;monkeypatch.setattr(e.time,'sleep',lambda _:None)
    monkeypatch.setattr(e,'_SIG_LIMIT',2)
    requests=[]
    def rpc(method,params):
        requests.append((method,params))
        if method=='getSignaturesForAddress':
            if params[1].get('before')=='early':return []
            return [{'signature':'late','blockTime':now-10},{'signature':'early','blockTime':now-20}]
        return {'transaction':{'signatures':[params[0]]},'blockTime':now-10 if params[0]=='late' else now-20,
                'meta':{'err':None,'preTokenBalances':[{'accountIndex':1,'owner':'wallet','mint':'mint','uiTokenAmount':{'uiAmount':5,'amount':'5','decimals':0}}],
                        'postTokenBalances':[{'accountIndex':1,'owner':'wallet','mint':'mint','uiTokenAmount':{'uiAmount':1,'amount':'1','decimals':0}}]}}
    monkeypatch.setattr(e,'_rpc',rpc)
    results=[]
    for day in range(3):
        monkeypatch.setattr(e.time,'time',lambda:now+day*86400)
        reserve=lambda:e.reserve_daily_call(str(p),f'2026-11-{23+day}',2)
        results.append(e._wallet_sold_after('wallet','mint',1,[1],detailed=True,reserve=reserve,progress_path=str(p)))
    assert results==[{'sold_ts':now-10,'complete':False},{'sold_ts':now-20,'complete':True},{'sold_ts':now-20,'complete':True}]
    assert [params[0] for method,params in requests if method=='getTransaction']==['late','early']
    pages=[params[1] for method,params in requests if method=='getSignaturesForAddress']
    assert pages==[{'limit':2},{'limit':2,'before':'early'}]


@pytest.mark.parametrize('mutation',['amount','signature','time','owner','index'])
def test_progress_rejects_unbound_or_malformed_transaction(tmp_path,monkeypatch,mutation):
    p=tmp_path/'copy.json';tx={'transaction':{'signatures':['sig']},'blockTime':10,
        'meta':{'err':None,'preTokenBalances':[{'accountIndex':1,'owner':'w','mint':'m','uiTokenAmount':{'uiAmount':5,'amount':'5','decimals':0}}], 'postTokenBalances':[]}}
    if mutation=='amount':tx['meta']['preTokenBalances'][0]['uiTokenAmount']['amount']='bad'
    elif mutation=='signature':tx['transaction']['signatures']=['wrong']
    elif mutation=='time':tx['blockTime']=11
    elif mutation=='owner':tx['meta']['preTokenBalances'][0].pop('owner')
    else:tx['meta']['preTokenBalances'][0].pop('accountIndex')
    monkeypatch.setattr(e,'_rpc',lambda method,args:[{'signature':'sig','blockTime':10}] if method=='getSignaturesForAddress' else tx)
    assert e._wallet_sold_after('w','m',1,[1],detailed=True,progress_path=str(p))=={'sold_ts':None,'complete':False}


@pytest.mark.parametrize('mutation',['version','digest','date','earliest','source'])
def test_corrupt_cached_progress_cannot_return_positive_or_negative(tmp_path,monkeypatch,mutation):
    import hashlib
    from daemon.launch_scanner import learning_effects as effects
    p=tmp_path/'copy.json';key=hashlib.sha256(effects._encoded(['w','m',1]).encode()).hexdigest()
    row={'source':['w','m',1],'state':{'before':None,'queue':[],'done':True,'earliest':10,'completed_day':'2026-11-23'}}
    doc={'version':1,'value':{key:row},'effects':{}}
    if mutation=='version':doc['version']=True
    elif mutation=='digest':doc['effects']={'x':'broken'}
    elif mutation=='date':row['state']['completed_day']='bad'
    elif mutation=='earliest':row['state']['earliest']=-1
    else:row['source'][0]='other'
    (tmp_path/'copy.json.progress.json').write_text(json.dumps(doc))
    monkeypatch.setattr(e,'_rpc',lambda *a:pytest.fail('must refuse cache without RPC'))
    assert e._wallet_sold_after('w','m',1,[1],detailed=True,progress_path=str(p))=={'sold_ts':None,'complete':False}


def test_raw_unit_sale_despite_equal_rounded_ui_and_none():
    tx={'transaction':{'signatures':['s']},'blockTime':10,'meta':{'preTokenBalances':[], 'postTokenBalances':[]}}
    def row(amount,decimals=9,owner='w',ui=10000000000.):
        return {'accountIndex':1,'owner':owner,'mint':'m','uiTokenAmount':{'amount':amount,'decimals':decimals,'uiAmount':ui}}
    tx['meta']['preTokenBalances']=[row('10000000000000000001')]
    tx['meta']['postTokenBalances']=[row('10000000000000000000',ui=None)]
    assert e._verified_token_delta(tx,{'signature':'s','blockTime':10},'w','m')==-1
    tx['meta']['postTokenBalances']=[row('10000000000000000000',decimals=8)]
    with pytest.raises(ValueError):e._verified_token_delta(tx,{'signature':'s','blockTime':10},'w','m')
    tx['meta']['postTokenBalances']=[row('10000000000000000000',owner='other')]
    with pytest.raises(ValueError):e._verified_token_delta(tx,{'signature':'s','blockTime':10},'w','m')


def test_verified_irrelevant_transaction_advances_queue(tmp_path,monkeypatch):
    tx={'transaction':{'signatures':['s']},'blockTime':10,'meta':{'err':None,'preTokenBalances':[], 'postTokenBalances':[]}}
    monkeypatch.setattr(e,'_rpc',lambda method,args:[{'signature':'s','blockTime':10}] if method=='getSignaturesForAddress' else tx)
    result=e._wallet_sold_after('w','m',1,[1],detailed=True,progress_path=str(tmp_path/'copy.json'))
    assert result=={'sold_ts':None,'complete':True}
