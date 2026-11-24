import json,copy,subprocess,sys,os
from types import SimpleNamespace
import pytest
from daemon.launch_scanner import fingerprints as fp,input_availability as ia,LaunchScanner,backtest


def stats():return dict(usd_market_cap=1.,price_usd=1.,vol_m5=1.,buys_m5=1,sells_m5=1,usd_liquidity=1.)


def tracker(tmp_path,**kw):return fp.FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'journal'),**kw))


@pytest.mark.parametrize('value,state',[(0,'recorded'),(None,'null'),('0','invalid-type'),(False,'invalid-type'),(float('nan'),'nonfinite'),(-1,'negative')])
def test_raw_tracker_input_distinction_before_arithmetic(tmp_path,value,state):
    tr=tracker(tmp_path);s=stats();s['usd_market_cap']=value;tr.observe('M',s,100.)
    row=json.loads((tmp_path/'journal').read_text());e=row['input_evidence']
    assert e['availability']['usd_market_cap']==state
    expected='recorded' if state=='recorded' else 'invalid-observation' if state in ('negative','nonfinite') else 'required-input-unavailable'
    assert tr.historical_inputs('M',200.)['state']==expected
    assert tr.historical_inputs('M',200.)['rows']==([] if state!='recorded' else [{'obs':row['obs'],'evidence':e}])


def test_missing_vs_optional_holder_and_explicit_zero(tmp_path):
    tr=tracker(tmp_path);s=stats();tr.observe('M',s,100.,holders=None)
    assert tr.historical_inputs('M',200.)['state']=='recorded'
    s.pop('vol_m5');tr.observe('M',s,110.)
    assert tr.historical_inputs('M',200.)['state']=='required-input-unavailable'


@pytest.mark.parametrize('kind',['mint','tuple','map','id','version','duplicate','partial'])
def test_stale_mismatched_unknown_envelope_refuses_not_repairs(tmp_path,kind):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);path=tmp_path/'journal';r=json.loads(path.read_text())
    if kind=='mint':r['input_evidence']['mint']='OTHER'
    if kind=='tuple':r['obs'][1]=99
    if kind=='map':r['input_evidence']['availability']['price_usd']='missing'
    if kind=='id':r['input_evidence']['observation_id']='stale'
    if kind=='version':r['input_evidence']['version']=999
    path.write_text(json.dumps(r)+'\n'+(json.dumps(r)+'\n' if kind=='duplicate' else '{' if kind=='partial' else ''))
    before=path.read_bytes();result=tr.historical_inputs('M',200.)
    assert result['state']!='recorded' and path.read_bytes()==before


def test_equal_times_distinct_ids_prune_and_legacy_window(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);tr.observe('M',stats(),100.)
    rows=tr.historical_inputs('M',200.)['rows'];assert rows[0]['evidence']['observation_id']!=rows[1]['evidence']['observation_id']
    tr._journal_enabled=False;assert tr.historical_inputs('M',200.)['state']=='recorded'
    tr._series['M'].append(tuple(tr._series['M'][0]));assert tr.historical_inputs('M',200.)['state']=='legacy-unrecorded'
    tr.prune(10000.);assert tr.historical_inputs('M',10000.)['state']=='empty'


def test_legacy_journal_never_backfilled_and_other_mint_isolated(tmp_path):
    tr=tracker(tmp_path);tr.observe('GOOD',stats(),100.);path=tmp_path/'journal';path.write_text(path.read_text()+json.dumps(dict(mint='OLD',obs=[100,1,1,1,1,1,1,None]))+'\n')
    assert tr.historical_inputs('GOOD',200.)['state']=='recorded'
    assert tr.historical_inputs('OLD',200.)['state']=='legacy-unrecorded'


def test_compaction_preserves_envelope_verbatim(tmp_path,monkeypatch):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);path=tmp_path/'journal';raw=path.read_text()
    monkeypatch.setattr(fp,'_COMPACT_MIN_BYTES',0);tr._journal_compact(101.)
    assert path.read_text()==raw
    assert tr.historical_inputs('M',200.)['state']=='recorded'


@pytest.mark.parametrize('mutation',[{},dict(usd_market_cap=None),dict(usd_market_cap='2'),dict(price_usd='bad'),dict(usd_liquidity=False)])
def test_realtime_arithmetic_and_scores_unchanged_for_partial_malformed_input(tmp_path,mutation,monkeypatch):
    tr=tracker(tmp_path);baseline=tracker(tmp_path/'other');baseline._journal_enabled=False
    # Compare new tuples against the exact prior conversion expression.
    for i in range(3):
        s=stats();s.update(mutation);tr.observe('M',s,100.+i)
        old=(float(100+i),fp._f(s.get('usd_market_cap')),fp._f(s.get('price_usd')),fp._f(s.get('vol_m5')),
             float(s.get('buys_m5') or 0),float(s.get('sells_m5') or 0),fp._f(s.get('usd_liquidity')),None)
        from collections import deque
        baseline._series.setdefault('M',deque(maxlen=baseline.window)).append(old)
        assert tuple(tr._series['M'][-1])==old
    assert tr.score('M')==baseline.score('M')


def test_judge_and_replay_explicit_unknown_blocks_without_discarding_record(tmp_path):
    tr=tracker(tmp_path);s=stats();s.pop('vol_m5');tr.observe('M',s,100.)
    cfg=tr.cfg;cfg.backtest_dataset_path=str(tmp_path/'history');sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text());assert r['snapshot_input_evidence']['state']=='required-input-unavailable'
    report=backtest.BacktestEngine().run([r]);assert report['n_histories']==1 and report['per_gate_blocked']['snapshot-input-unavailable']==1


def test_process_restart_recovers_same_envelope(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);expected=tr.historical_inputs('M',200.)
    code='from daemon.launch_scanner.fingerprints import FingerprintTracker;from types import SimpleNamespace;import json,sys;t=FingerprintTracker(SimpleNamespace(fp_series_journal_path=sys.argv[1]));print(json.dumps(t.historical_inputs("M",200.)))'
    p=subprocess.run([sys.executable,'-c',code,str(tmp_path/'journal')],capture_output=True,text=True,check=True)
    assert json.loads(p.stdout)==expected


def test_paper_ordinary_and_pinned_recovery_preserve_unknown_snapshot_economics(tmp_path,account,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import close_outbox
    a,store,state,sc,cfg=setup(tmp_path);mint=state['degen_positions']['M']['mint']
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(tmp_path/'paper')
    cfg.degen.fp_series_journal_path=str(tmp_path/'journal')
    tr=fp.FingerprintTracker(cfg.degen);s=stats();s.pop('vol_m5');tr.observe(mint,s,100.)
    sc._fp_tracker=tr
    monkeypatch.setattr(sc,'_backtest_record_paper_close',lambda *a,**k:None)
    sc._manage_exits(cfg,a,state)
    r=json.loads((tmp_path/'paper').read_text());e=store.journal_query(kind='scanner_close_committed')[0]['detail']
    assert r['native_allocation']['realized_pnl_sol']==e['native_receipt']['realized_pnl_sol']
    assert r['snapshot_input_evidence']['state']=='required-input-unavailable'
    assert backtest.BacktestEngine().run([r])['paper']['trades']==1
    assert close_outbox.rebuild_backtest(e)['record']['snapshot_input_evidence']==r['snapshot_input_evidence']


def test_permission_error_never_uses_plausible_window(tmp_path,monkeypatch):
    import builtins
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);real=builtins.open
    def denied(path,*a,**kw):
        if str(path)==str(tmp_path/'journal'):raise PermissionError('fixture')
        return real(path,*a,**kw)
    monkeypatch.setattr(builtins,'open',denied)
    assert tr.historical_inputs('M',200.)['state']=='journal-unavailable'


def test_concurrent_writer_same_row_identity_binding(tmp_path):
    code='from daemon.launch_scanner.fingerprints import FingerprintTracker;from types import SimpleNamespace;import sys;t=FingerprintTracker(SimpleNamespace(fp_series_journal_path=sys.argv[1]));[t.observe(sys.argv[2],dict(usd_market_cap=1,price_usd=1,vol_m5=1,buys_m5=1,sells_m5=1,usd_liquidity=1),100+i) for i in range(10)]'
    ps=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path/'journal'),m]) for m in ('A','B')]
    assert [p.wait(timeout=15) for p in ps]==[0,0]
    tr=tracker(tmp_path)
    for m in ('A','B'):
        result=tr.historical_inputs(m,200.);assert result['state']=='recorded' and len(result['rows'])==10
    rows=[json.loads(l) for l in (tmp_path/'journal').read_text().splitlines()]
    assert len({r['input_evidence']['observation_id'] for r in rows})==20


def test_partial_append_survives_compaction_and_refuses(tmp_path,monkeypatch):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);path=tmp_path/'journal';path.write_text(path.read_text()+'{\n')
    monkeypatch.setattr(fp,'_COMPACT_MIN_BYTES',0);tr._journal_compact(101.)
    assert '{\n' in path.read_text() and tr.historical_inputs('M',200.)['state']=='unattributable-corruption'


from tests.test_paper_account_instances import account


@pytest.mark.parametrize('bad',[None,'200',True,float('nan'),float('inf'),-1,10**1000])
def test_record_time_strict_not_fabricated(tmp_path,bad):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.)
    assert tr.historical_inputs('M',bad)['state']=='record-time-unavailable'


def test_valid_judge_source_exact_binding_to_replay_snapshots(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.)
    cfg=tr.cfg;cfg.backtest_dataset_path=str(tmp_path/'history');sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text());assert r['snapshot_input_evidence']['state']=='recorded'
    original=backtest.BacktestEngine().run([r]);assert 'snapshot-input-unavailable' not in original['per_gate_blocked']
    changed=copy.deepcopy(r);changed['snapshots'][0]['price_usd']=999
    assert backtest.BacktestEngine().run([changed])['per_gate_blocked']['snapshot-input-unavailable']==1


def test_missing_envelope_same_window_never_upgraded(tmp_path):
    tr=tracker(tmp_path,fp_series_journal_enabled=False);tr.observe('M',stats(),100.)
    original=tr._series['M'][0]
    tr._series['M'][0]=tuple(original)
    assert tr.historical_inputs('M',200.)['state']=='legacy-unrecorded'


def test_optional_explicit_holder_zero_distinguished_from_null(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.,holders=0)
    e=tr.historical_inputs('M',200.)
    assert e['state']=='recorded' and e['rows'][0]['evidence']['availability']['holders']=='recorded'
    # Existing numerical behavior remains None. Holder raw availability is a
    # separate diagnostic, never used as a new observed holder-count series.
    assert e['rows'][0]['obs'][7] is None


@pytest.mark.parametrize('kind',['shape','bool','negative','nonfinite','count','reverse'])
def test_self_consistent_digest_does_not_bypass_numeric_validation(tmp_path,kind):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);tr.observe('M',stats(),101.)
    e=tr.historical_inputs('M',200.);r=e['rows'][0];o=r['obs']
    if kind=='shape':o.pop()
    elif kind=='bool':o[1]=True
    elif kind=='negative':o[1]=-1
    elif kind=='nonfinite':o[1]=float('inf')
    elif kind=='count':o[4]=1.5
    else:e['rows'].reverse()
    r['evidence']['binding']=ia.digest('M',o,r['evidence']['observation_id'],r['evidence']['availability'])
    assert not ia.matches('M',e,[])


def test_mixed_same_mint_versions_never_backfilled(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);path=tmp_path/'journal'
    r=json.loads(path.read_text());r.pop('input_evidence');r['obs'][0]=101
    path.write_text(path.read_text()+json.dumps(r)+'\n')
    assert tr.historical_inputs('M',200.)['state']=='legacy-unrecorded'


def test_window_episode_filter_is_same_as_journal(tmp_path):
    tr=tracker(tmp_path)
    for t in (100.,101.,102.):tr.observe('M',stats(),t)
    expected=tr.historical_inputs('M',200.,since=101.)['rows'];tr._journal_enabled=False
    assert tr.historical_inputs('M',200.,since=101.)['rows']==expected


def test_pinned_recorded_envelope_must_match_mint_and_snapshots(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.)
    sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    from daemon.launch_scanner import close_outbox as co
    source=co.backtest_source(sc,SimpleNamespace(backtest_dataset_path=str(tmp_path/'hist')),{},'M',200.)
    assert source['state']=='recorded'
    with pytest.raises(ValueError):co.validate_backtest_source(source,200.,'OTHER')
    source['snapshots'][0]['price_usd']=123
    with pytest.raises(ValueError):co.validate_backtest_source(source,200.,'M')


def test_explicit_null_evidence_never_takes_legacy_compatibility_path():
    result=backtest.BacktestEngine().run([{'mint':'M','snapshot_input_evidence':None}])
    assert result['per_gate_blocked']['snapshot-input-unavailable']==1
    legacy=backtest.BacktestEngine().run([{'mint':'M'}])
    assert legacy['n_snapshot_inputs_legacy_unrecorded']==1


def test_compaction_and_two_process_append_share_stable_lock(tmp_path):
    tr=tracker(tmp_path);tr.observe('A',stats(),100.)
    code='from daemon.launch_scanner import fingerprints as f;from types import SimpleNamespace;import sys;f._COMPACT_MIN_BYTES=0;t=f.FingerprintTracker(SimpleNamespace(fp_series_journal_path=sys.argv[1]));[(t.observe(sys.argv[2],dict(usd_market_cap=1,price_usd=1,vol_m5=1,buys_m5=1,sells_m5=1,usd_liquidity=1),100+i),t._journal_compact(101+i)) for i in range(10)]'
    ps=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path/'journal'),m]) for m in ('B','C')]
    assert [p.wait(timeout=15) for p in ps]==[0,0]
    for m,n in (('A',1),('B',10),('C',10)):
        result=tr.historical_inputs(m,200.);assert result['state']=='recorded' and len(result['rows'])==n


def test_window_invalid_tuple_with_matching_envelope_refuses(tmp_path):
    tr=tracker(tmp_path,fp_series_journal_enabled=False);tr.observe('M',stats(),100.)
    r=tr._series['M'][0];e=json.loads(r.evidence_json);o=list(r);o[4]=1.5
    e['binding']=ia.digest('M',o,e['observation_id'],e['availability']);tr._series['M'][0]=ia.Observation(o,e)
    assert tr.historical_inputs('M',200.)['state']=='invalid-observation'


@pytest.mark.parametrize('holder',[None,0,'0',False,-1,float('nan')])
def test_optional_holder_diagnostic_never_erases_price_input_evidence(tmp_path,holder):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.,holders=holder)
    e=tr.historical_inputs('M',200.)
    assert e['state']=='recorded'
    snapshots=[dict(t=100.,mcap_usd=1.,price_usd=1.,vol5m_usd=1.,buys5m=1,sells5m=1,liq_usd=1.)]
    assert ia.matches('M',e,snapshots)


@pytest.mark.parametrize('holder',[None,0,'0',False,-1,float('nan')])
def test_actual_judge_keeps_price_history_when_optional_holder_unusable(tmp_path,holder):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.,holders=holder)
    cfg=tr.cfg;cfg.backtest_dataset_path=str(tmp_path/'history');sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text())
    assert r['snapshots'][0]['price_usd']==1 and r['snapshot_input_evidence']['state']=='recorded'
    assert 'snapshot-input-unavailable' not in backtest.BacktestEngine().run([r])['per_gate_blocked']


@pytest.mark.parametrize('bad',[50.,None,'200',True,float('nan'),float('inf'),-1,10**1000])
def test_actual_judge_replay_record_boundary_must_match_capture(tmp_path,bad):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.)
    cfg=tr.cfg;cfg.backtest_dataset_path=str(tmp_path/'history');sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80},safety_eval_t=100),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text());r['outcome'].update(recorded_t=bad,end_t=bad,peak_t=bad)
    assert backtest.BacktestEngine().run([r])['per_gate_blocked']['snapshot-input-unavailable']==1


def test_capture_binds_recordtime_mint_order_and_result(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);tr.observe('M',stats(),101.)
    result=tr.historical_inputs('M',200.)
    assert ia.capture_time('M',result)==200.
    for mutation in ('time','mint','order','state'):
        changed=copy.deepcopy(result)
        if mutation=='time':changed['capture']['recorded_t']=50.
        elif mutation=='mint':changed['capture']['mint']='OTHER'
        elif mutation=='order':changed['rows'].reverse()
        else:changed['state']='empty'
        assert ia.capture_time('M',changed) is None


def test_actual_recorded_envelope_ordinary_commit_equals_pinned_rebuild(tmp_path,account):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import close_outbox as co
    a,store,state,scanner,cfg=setup(tmp_path);mint=state['degen_positions']['M']['mint']
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(tmp_path/'history')
    cfg.degen.fp_series_journal_path=str(tmp_path/'journal')
    tr=fp.FingerprintTracker(cfg.degen);tr.observe(mint,stats(),100.);scanner._fp_tracker=tr
    scanner._manage_exits(cfg,a,state)
    evidence=store.journal_query(kind='scanner_close_committed')[0]['detail']
    ordinary=evidence['backtest_spec']['record'];rebuilt=co.rebuild_backtest(evidence)['record']
    assert ordinary['snapshot_input_evidence']['state']=='recorded'
    assert ordinary['snapshot_input_evidence']==rebuilt['snapshot_input_evidence']
    assert ordinary['snapshots']==rebuilt['snapshots']
    assert ordinary['native_allocation']==rebuilt['native_allocation']
    assert ia.capture_time(mint,ordinary['snapshot_input_evidence'])==evidence['context']['decision_ts']
    assert json.loads((tmp_path/'history').read_text())['snapshot_input_evidence']==ordinary['snapshot_input_evidence']


def test_self_consistent_capture_still_refuses_future_observation(tmp_path):
    tr=tracker(tmp_path);tr.observe('M',stats(),100.)
    result=tr.historical_inputs('M',200.)
    changed=ia.capture('M',50.,{k:v for k,v in result.items() if k!='capture'})
    snaps=[dict(t=100.,mcap_usd=1.,price_usd=1.,vol5m_usd=1.,buys5m=1,sells5m=1,liq_usd=1.)]
    assert not ia.matches('M',changed,snaps,until=50.)


def test_actual_window_episode_pinned_recovery_keeps_economics(tmp_path,account,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import close_outbox as co
    a,store,state,scanner,cfg=setup(tmp_path);mint=state['degen_positions']['M']['mint']
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(tmp_path/'history')
    cfg.degen.fp_series_journal_path=None
    tr=tracker(tmp_path);tr.observe(mint,stats(),100.);tr.observe(mint,stats(),110.);scanner._fp_tracker=tr
    state['seen']={mint:{'first_ts':105.}}
    source=co.backtest_source(scanner,cfg.degen,state,mint,200.)
    assert source['state']=='recorded' and [s['t'] for s in source['snapshots']]==[110.]
    assert ia.matches(mint,source['input_evidence'],source['snapshots'],until=200.)
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**k:None)
    scanner._manage_exits(cfg,a,state)
    r=json.loads((tmp_path/'history').read_text());e=store.journal_query(kind='scanner_close_committed')[0]['detail']
    assert [s['t'] for s in r['snapshots']]==[110.]
    assert r['native_allocation']['realized_pnl_sol']==e['native_receipt']['realized_pnl_sol']
    assert r['snapshot_input_evidence']['state']=='recorded'


def test_concurrent_cross_read_mismatch_retains_empty_economic_source(tmp_path,monkeypatch):
    from daemon.launch_scanner import close_outbox as co
    tr=tracker(tmp_path);tr.observe('M',stats(),100.);sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    real=tr.historical_inputs
    def appended(*a,**k):
        tr.observe('M',stats(),110.);return real(*a,**k)
    monkeypatch.setattr(tr,'historical_inputs',appended)
    source=co.backtest_source(sc,SimpleNamespace(backtest_dataset_path=str(tmp_path/'hist')),{},'M',200.)
    assert source['state']=='recorded' and source['snapshots']==[] and source['snapshots_state']=='unavailable'
    assert source['input_evidence']['state']=='source-snapshot-mismatch'
