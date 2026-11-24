import copy,json
from types import SimpleNamespace
import pytest
from daemon.launch_scanner import sources as S,provider_inputs as P,input_availability as I,LaunchScanner,backtest
from daemon.launch_scanner.fingerprints import FingerprintTracker
from tests.test_paper_account_instances import account


def payload(mint='M',chain='solana'):
    return dict(baseToken={'address':mint},quoteToken={'address':'QUOTE'},chainId=chain,pairAddress='PAIR',
                priceUsd='1.25',marketCap='100',liquidity={'usd':'10'},volume={'m5':'5'},txns={'m5':{'buys':2,'sells':0}})


def wrapped(monkeypatch,mint='M',pair=None,chain='solana',single=False):
    pair=payload(mint,chain) if pair is None else pair
    monkeypatch.setattr(S,'_get_json',lambda *a:{'pairs':[pair]} if single else [pair])
    return S.dexscreener_token_stats(mint,chain) if single else S.dexscreener_batch_stats([mint],chain)[mint]


def tracked(tmp_path,stats,mint='M'):
    tr=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'series')))
    tr.observe(mint,stats,100.)
    return tr,tr.historical_inputs(mint,200.)


@pytest.mark.parametrize('single',[False,True])
def test_actual_wrappers_tracker_journal_window_and_judge(tmp_path,monkeypatch,single):
    st=wrapped(monkeypatch,single=single);tr,result=tracked(tmp_path,st)
    assert P.history_status('M',result)=='provider-recorded'
    assert result['rows'][0]['evidence']['version']==3
    tr._journal_enabled=False;assert P.history_status('M',tr.historical_inputs('M',200.))=='provider-recorded'
    sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr;cfg=tr.cfg;cfg.backtest_dataset_path=str(tmp_path/'history')
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',chain='solana',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text());report=backtest.BacktestEngine(require_provider_inputs=True).run([r])
    assert report['provider_input_qualification']['cohort_qualified']
    assert 'provider-input-cohort-unavailable' not in report['per_gate_blocked']


@pytest.mark.parametrize('value,qualifies',[(0,True),('0',True),('1.25',True),(False,False),(None,False),('',False),(' 1',False),('+1',False),('1e2',False),('01',False),('bad',False),(-1,False),(float('inf'),False)])
def test_raw_provider_decimal_policy_and_same_old_price_arithmetic(tmp_path,monkeypatch,value,qualifies):
    p=payload();p['priceUsd']=value;st=wrapped(monkeypatch,pair=p)
    assert st['price_usd']==S._f(value)
    tr,result=tracked(tmp_path,st)
    assert (P.history_status('M',result)=='provider-recorded') is qualifies
    raw=st[P.KEY]['fields']['price_usd'];assert raw['value']==value


@pytest.mark.parametrize('value',[1.5,'1.5',True,2**54+1])
def test_count_truncation_or_precision_not_qualified(tmp_path,monkeypatch,value):
    p=payload();p['txns']['m5']['buys']=value
    if isinstance(value,str):
        with pytest.raises(ValueError):wrapped(monkeypatch,pair=p)
        return  # unchanged int(string-fraction) exception, never fabricated evidence
    st=wrapped(monkeypatch,pair=p)
    _,result=tracked(tmp_path,st);assert P.history_status('M',result)!='provider-recorded'


@pytest.mark.parametrize('primary',[None,0,''])
def test_market_cap_proxy_is_not_observed_market_cap(tmp_path,monkeypatch,primary):
    p=payload();p['marketCap']=primary;p['fdv']='999';st=wrapped(monkeypatch,pair=p)
    _,result=tracked(tmp_path,st);assert st['usd_market_cap']==999
    assert P.history_status('M',result)=='provider-market-cap-proxy'


@pytest.mark.parametrize('mutation',['mint','quote-direction','chain','absent-mint','absent-chain','absent-pair','output','path','lost','binding'])
def test_conflicting_absent_or_dropped_envelope_never_qualifies(tmp_path,monkeypatch,mutation):
    p=payload()
    if mutation=='mint':p['baseToken']['address']='OTHER'
    elif mutation=='quote-direction':p['baseToken']['address']='QUOTE';p['quoteToken']['address']='M'
    elif mutation=='chain':p['chainId']='base'
    elif mutation=='absent-mint':p.pop('baseToken')
    elif mutation=='absent-chain':p.pop('chainId')
    elif mutation=='absent-pair':p.pop('pairAddress')
    st=wrapped(monkeypatch,pair=p,single=True)
    if mutation=='output':st['price_usd']=99
    elif mutation=='path':st[P.KEY]['fields']['price_usd']['path']='fake';st[P.KEY]=P.sealed(st[P.KEY])
    elif mutation=='lost':st.pop(P.KEY)
    elif mutation=='binding':st[P.KEY]['binding']='stale'
    _,result=tracked(tmp_path,st);assert P.history_status('M',result)!='provider-recorded'


def test_duplicate_pair_choice_and_alias_identity_explicit(tmp_path,monkeypatch):
    low=payload('0xAb','eth');high=payload('0xab','eth');high['liquidity']['usd']='99';high['pairAddress']='HIGH'
    monkeypatch.setattr(S,'_get_json',lambda *a:[low,high])
    st=S.dexscreener_batch_stats(['0xAb'],'ethereum')['0xAb'];_,result=tracked(tmp_path,st,'0xAb')
    assert st[P.KEY]['payload_identity']['pair']['value']=='HIGH'
    assert P.history_status('0xAb',result)=='provider-recorded'


def test_cohort_refusal_does_not_drop_paper_loss(tmp_path,monkeypatch):
    st=wrapped(monkeypatch);tr,result=tracked(tmp_path,st)
    paper={'outcome_kind':'paper','mint':'OLD','pnl_sol':-2.,'realized_mult':0.,'snapshot_input_evidence':None}
    report=backtest.BacktestEngine(require_provider_inputs=True).run([paper,{'mint':'M','snapshot_input_evidence':result}])
    assert report['paper']['trades']==1 and report['paper']['total_pnl_sol']==-2.
    assert not report['provider_input_qualification']['cohort_qualified']
    assert report['per_gate_blocked']['provider-input-cohort-unavailable']==1


@pytest.mark.parametrize('failure',['none','invalid','lost','wrong-mint','wrong-chain'])
def test_actual_wrapper_scanner_paper_projected_ordinary_and_pinned_economics(tmp_path,account,monkeypatch,failure):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import close_outbox as co
    a,store,state,scanner,cfg=setup(tmp_path);mint=state['degen_positions']['M']['mint']
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(tmp_path/'history')
    cfg.degen.fp_series_journal_path=str(tmp_path/'series')
    st=wrapped(monkeypatch,mint=mint)
    if failure=='invalid':st[P.KEY]['binding']='bad'
    elif failure=='lost':st.pop(P.KEY)
    elif failure in ('wrong-mint','wrong-chain'):
        st[P.KEY]['request']['mint' if failure=='wrong-mint' else 'chain']='OTHER' if failure=='wrong-mint' else 'base'
        st[P.KEY]=P.sealed(st[P.KEY])
    scanner._fp_holder_refresh=lambda *a:None
    scanner._fingerprint_track(cfg,state,{'mint':mint},st,100.)
    if failure!='none':monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**k:None)
    scanner._manage_exits(cfg,a,state)
    event=store.journal_query(kind='scanner_close_committed')[0]['detail'];record=json.loads((tmp_path/'history').read_text())
    assert record['native_allocation']['realized_pnl_sol']==event['native_receipt']['realized_pnl_sol']
    rebuilt=co.rebuild_backtest(event)['record']
    assert record['snapshot_input_evidence']==rebuilt['snapshot_input_evidence']
    status=P.history_status(mint,record['snapshot_input_evidence'])
    assert (status=='provider-recorded') is (failure=='none')
    report=backtest.BacktestEngine(require_provider_inputs=True).run([record])
    assert report['paper']['trades']==1
    assert report['provider_input_qualification']['cohort_qualified'] is (failure=='none')
    assert len(store.journal_query(kind='scanner_backtest_projected'))==1


def test_gecko_actual_wrapper_identity_separate_from_request(tmp_path,monkeypatch):
    token={'id':'solana_M','attributes':{'address':'M','price_usd':'1','market_cap_usd':'100','total_reserve_in_usd':'10'}}
    monkeypatch.setattr(S,'_gt_budget_ok',lambda:True);monkeypatch.setattr(S,'_gt_get_json',lambda *a:{'data':[token]})
    st=S.geckoterminal_token_stats(['M'])['M'];tr,result=tracked(tmp_path,st)
    assert st[P.KEY]['request']['mint']=='M' and st[P.KEY]['payload_identity']['resource']['value']=='solana_M'
    assert P.history_status('M',result)!='provider-recorded'  # absent transaction windows
    token.pop('id');st=S.geckoterminal_token_stats(['M'])['M']
    assert st[P.KEY]['payload_identity']['resource']['state']=='missing'


def test_source_envelope_restart_compact_and_mixed_scope(tmp_path,monkeypatch):
    st=wrapped(monkeypatch);tr,result=tracked(tmp_path,st)
    fresh=FingerprintTracker(tr.cfg);assert P.history_status('M',fresh.historical_inputs('M',200.))=='provider-recorded'
    import daemon.launch_scanner.fingerprints as fp
    monkeypatch.setattr(fp,'_COMPACT_MIN_BYTES',0);tr._journal_compact(101.)
    assert P.history_status('M',fresh.historical_inputs('M',200.))=='provider-recorded'
    tr.observe('M',{k:v for k,v in st.items() if k!=P.KEY},110.)
    assert P.history_status('M',fresh.historical_inputs('M',200.))=='provider-unrecorded'


@pytest.mark.parametrize('mutation',[{}, {'priceUsd':None},{'priceUsd':'bad'},{'marketCap':0,'fdv':1}])
def test_added_adapter_metadata_does_not_change_realtime_tuple_score(tmp_path,monkeypatch,mutation):
    p=payload();p.update(mutation);st=wrapped(monkeypatch,pair=p)
    tr=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'one')))
    old=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'two')))
    bare={k:v for k,v in st.items() if k!=P.KEY}
    for t in (100.,101.,102.):tr.observe('M',st,t);old.observe('M',bare,t)
    assert list(tr._series['M'])==list(old._series['M']) and tr.score('M')==old.score('M')


@pytest.mark.parametrize('p',[{'txns':{'m5':{'buys':'bad'}}},{'txns':{'m5':{'buys':'1.5'}}},{'liquidity':['invalid-container']},{'txns':{'m5':{'buys':float('inf')}}}])
def test_old_normalizer_exception_behavior_is_not_hidden(p):
    with pytest.raises((ValueError,AttributeError,OverflowError)):S._pair_to_stats(p)


def test_provider_scope_partial_append_permission_and_independent_mint(tmp_path,monkeypatch):
    st=wrapped(monkeypatch);tr,result=tracked(tmp_path,st);tr.observe('GOOD',wrapped(monkeypatch,mint='GOOD'),101.)
    with open(tmp_path/'series','a') as f:f.write(json.dumps({'mint':'OTHER','obs':['bad']})+'\n')
    assert P.history_status('GOOD',tr.historical_inputs('GOOD',200.))=='provider-recorded'
    with open(tmp_path/'series','a') as f:f.write('{')
    assert P.history_status('GOOD',tr.historical_inputs('GOOD',200.))=='tracker-unavailable'
    import builtins
    real=builtins.open
    def denied(path,*a,**k):
        if str(path)==str(tmp_path/'series'):raise PermissionError('fixture')
        return real(path,*a,**k)
    monkeypatch.setattr(builtins,'open',denied)
    assert P.history_status('M',tr.historical_inputs('M',200.))=='tracker-unavailable'


def test_provider_scope_sourceuuid_pruning_and_equal_times(tmp_path,monkeypatch):
    first=wrapped(monkeypatch);second=wrapped(monkeypatch)
    assert first[P.KEY]['source_id']!=second[P.KEY]['source_id']
    tr,result=tracked(tmp_path,first);tr.observe('M',second,100.)
    result=tr.historical_inputs('M',200.);assert P.history_status('M',result)=='provider-recorded'
    tr._journal_enabled=False;tr.prune(100000.)
    assert P.history_status('M',tr.historical_inputs('M',100000.))=='tracker-unavailable'


def test_actual_wrapper_scanner_tracker_to_judge_provider_gate(tmp_path,monkeypatch):
    st=wrapped(monkeypatch);cfg=SimpleNamespace(degen=SimpleNamespace(fp_series_journal_path=str(tmp_path/'series'),
        backtest_dataset_path=str(tmp_path/'history')))
    sc=LaunchScanner.__new__(LaunchScanner);sc._fp_holder_refresh=lambda *a:None
    sc._fingerprint_track(cfg,{},dict(mint='M'),st,100.)
    sc._backtest_record(cfg,{},dict(mint='M',chain='solana',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text())
    assert backtest.BacktestEngine(require_provider_inputs=True).run([r])['provider_input_qualification']['cohort_qualified']
    r['chain']='base'
    assert not backtest.BacktestEngine(require_provider_inputs=True).run([r])['provider_input_qualification']['cohort_qualified']


def test_provider_envelope_fresh_process_and_concurrent_compaction(tmp_path):
    import subprocess,sys
    code='''from daemon.launch_scanner import sources as s,provider_inputs as p,fingerprints as f
from types import SimpleNamespace
import sys,json
f._COMPACT_MIN_BYTES=0
mint=sys.argv[2]
payload=dict(baseToken={'address':mint},chainId='solana',pairAddress='PAIR',priceUsd='1',marketCap='100',liquidity={'usd':'10'},volume={'m5':'5'},txns={'m5':{'buys':2,'sells':0}})
t=f.FingerprintTracker(SimpleNamespace(fp_series_journal_path=sys.argv[1]))
for i in range(5):
 st=p.resolved(s._pair_to_stats(payload),mint,'solana','highest-liquidity-base-token-pair')
 t.observe(mint,st,100+i)
 t._journal_compact(105)
print(p.history_status(mint,t.historical_inputs(mint,200)))
'''
    ps=[__import__('subprocess').Popen([sys.executable,'-c',code,str(tmp_path/'series'),m],stdout=subprocess.PIPE,text=True) for m in ('A','B')]
    assert [p.communicate(timeout=15)[0].strip() for p in ps]==['provider-recorded']*2
    tr=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'series')))
    for m in ('A','B'):
        result=tr.historical_inputs(m,200.)
        assert len(result['rows'])==5 and P.history_status(m,result)=='provider-recorded'


@pytest.mark.parametrize('value',['0.00000000000000000001','-0','1.234567890123456789','9'*400])
def test_decimal_precision_policy_is_visible_and_overflow_refused(value):
    number,state=P.parsed({'state':'present','value':value})
    if len(value)>300:assert state=='overflow'
    else:assert state=='parsed-decimal-string' and number==float(value)


def test_tracker_binds_copied_adapter_envelope_against_later_mutation(tmp_path,monkeypatch):
    st=wrapped(monkeypatch);tr,result=tracked(tmp_path,st)
    st[P.KEY]['fields']['price_usd']['value']='99'
    assert P.history_status('M',tr.historical_inputs('M',200.))=='provider-recorded'
    result['rows'][0]['evidence']['provider_normalization']['fields']['price_usd']['value']='99'
    result=I.capture('M',200.,{k:v for k,v in result.items() if k!='capture'})
    assert P.history_status('M',result)=='provider-invalid'


@pytest.mark.parametrize('model',[backtest.REPLAY_MODEL_V2,'v1-legacy'])
@pytest.mark.parametrize('boundary',[50.,None,'200',True,float('nan')])
def test_actual_qualified_replay_boundary_independent_of_model(tmp_path,monkeypatch,model,boundary):
    st=wrapped(monkeypatch);tr,_=tracked(tmp_path,st);sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    tr.cfg.backtest_dataset_path=str(tmp_path/'history')
    sc._backtest_record(SimpleNamespace(degen=tr.cfg),{},dict(mint='M',chain='solana',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text());r['outcome']['recorded_t']=boundary
    report=backtest.BacktestEngine(replay_model=model,require_provider_inputs=True).run([r])
    assert not report['provider_input_qualification']['cohort_qualified']
    assert report['n_trades']==0 and report['per_gate_blocked']['provider-input-cohort-unavailable']==1


def test_paper_source_boundary_is_original_decision_not_close_and_tamper_refuses(tmp_path,account,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import close_outbox as co
    a,store,state,scanner,cfg=setup(tmp_path);mint=state['degen_positions']['M']['mint']
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(tmp_path/'history');cfg.degen.fp_series_journal_path=str(tmp_path/'series')
    st=wrapped(monkeypatch,mint=mint);scanner._fp_holder_refresh=lambda *a:None
    scanner._fingerprint_track(cfg,state,{'mint':mint},st,100.)
    scanner._manage_exits(cfg,a,state)
    event=store.journal_query(kind='scanner_close_committed')[0]['detail'];r=json.loads((tmp_path/'history').read_text())
    assert r['snapshot_recorded_t']==event['context']['decision_ts']
    assert I.capture_time(mint,r['snapshot_input_evidence'])==r['snapshot_recorded_t']
    assert co.rebuild_backtest(event)['record']['snapshot_recorded_t']==r['snapshot_recorded_t']
    assert P.record_status(r)=='provider-recorded'
    r['snapshot_recorded_t']=50.
    report=backtest.BacktestEngine(require_provider_inputs=True,replay_model='v1-legacy').run([r])
    assert not report['provider_input_qualification']['cohort_qualified'] and report['paper']['trades']==1
    assert report['paper']['total_pnl_sol']==round(event['native_receipt']['realized_pnl_sol'],6)


def test_paper_deliberately_later_native_close_does_not_recapture(tmp_path,account,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import close_outbox as co
    from datetime import datetime,timezone
    a,store,state,scanner,cfg=setup(tmp_path);mint=state['degen_positions']['M']['mint']
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(tmp_path/'history');cfg.degen.fp_series_journal_path=str(tmp_path/'series')
    st=wrapped(monkeypatch,mint=mint);scanner._fp_holder_refresh=lambda *a:None
    scanner._fingerprint_track(cfg,state,{'mint':mint},st,100.)
    scanner._manage_exits(cfg,a,state)
    original=store.journal_query(kind='scanner_close_committed')[0]['detail'];e=copy.deepcopy(original)
    decision=e['context']['decision_ts'];later=decision+120.
    e['native_receipt']['ts']=datetime.fromtimestamp(later,timezone.utc).isoformat()
    later=datetime.fromisoformat(e['native_receipt']['ts']).timestamp()
    rebuilt=co.rebuild_backtest(e)['record']
    class Capture:
        def scanner_close_event(self,event,evidence):self.evidence=evidence;return False
    target=Capture();co.commit(target,e['context'],e['native_receipt']);normal=target.evidence['backtest_spec']['record']
    for r in (rebuilt,normal):
        assert r['outcome']['recorded_t']==later and r['t_close']==later
        assert r['snapshot_recorded_t']==decision and I.capture_time(mint,r['snapshot_input_evidence'])==decision
        assert P.record_status(r)=='provider-recorded'
    assert rebuilt['snapshot_input_evidence']==normal['snapshot_input_evidence']==original['backtest_spec']['record']['snapshot_input_evidence']


def test_strict_holdout_keeps_original_whole_cohort_and_paper_losses(tmp_path,monkeypatch):
    st=wrapped(monkeypatch);tr,_=tracked(tmp_path,st);sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    tr.cfg.backtest_dataset_path=str(tmp_path/'history')
    sc._backtest_record(SimpleNamespace(degen=tr.cfg),{},dict(mint='M',chain='solana',safety={'score':80},safety_eval_t=100),'confirm',200.)
    valid=json.loads((tmp_path/'history').read_text())
    invalid=backtest.synthetic_history(mint='OLD',n_snaps=10)
    paper={'outcome_kind':'paper','mint':'LOSS','snapshots':[],'pnl_sol':-3.,'realized_mult':0.}
    result=backtest.BacktestEngine(require_provider_inputs=True).run_with_holdout([valid,invalid,paper],.5)
    assert not result['provider_input_qualification']['cohort_qualified']
    for key in ('dev','eval','rejected'):
        q=result[key]['provider_input_qualification']
        assert q['required'] and not q['cohort_qualified'] and sum(q['histories_by_state'].values())==3
        assert result[key]['n_trades']==0
    assert result['rejected']['paper']['trades']==1 and result['rejected']['paper']['total_pnl_sol']==-3.


def test_strict_provider_option_actual_cli(tmp_path):
    import subprocess,sys
    path=tmp_path/'history';path.write_text(json.dumps(backtest.synthetic_history(n_snaps=10))+'\n')
    result=subprocess.run([sys.executable,'-m','daemon.launch_scanner.backtest',str(path),'--require-provider-inputs','--holdout','0.5'],capture_output=True,text=True)
    assert result.returncode==0 and 'PROVIDER INPUT COHORT: unavailable' in result.stdout
    assert 'provider-input-cohort-unavailable' in result.stdout
