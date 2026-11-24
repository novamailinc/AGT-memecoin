import json
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,close_outbox


def test_initial_builder_error_retries_pinned_inputs_after_flatbook(account,tmp_path,monkeypatch):
    path=tmp_path/'history.jsonl';a,store,state,scanner,cfg=setup(tmp_path)
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(path)
    real=LaunchScanner._backtest_record_paper_close
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**kw:None)
    with monkeypatch.context() as m:
        m.setattr(close_outbox,'rebuild_backtest',lambda *a:None)
        scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and not path.exists()
    cfg.degen.backtest_dataset_path=str(tmp_path/'wrong.jsonl');state['narrative_tags']={'changed':['wrong']}
    close_outbox.project_backtests(store);close_outbox.project_backtests(store)
    rows=[json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows)==1 and rows[0]['outcome_kind']=='paper'
    evidence=store.journal_query(kind='scanner_close_committed')[0]['detail']
    assert rows[0]['outcome_id']==close_outbox.key(evidence['context']['identity'])
    assert rows[0]['pnl_sol']==round(evidence['native_receipt']['realized_pnl_sol'],6)
    assert not (tmp_path/'wrong.jsonl').exists()


def test_missing_entry_uses_original_decision_fallback(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path);path=tmp_path/'history.jsonl'
    cfg.degen.backtest_dataset_path=str(path);state['degen_positions']['M']['entry_ts']=0
    state['degen_positions']['M']['sl']=.000002
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**kw:None)
    scanner._manage_exits(cfg,a,state)
    e=store.journal_query(kind='scanner_close_committed')[0]['detail']
    rec=json.loads(path.read_text())
    assert rec['t_entry']==e['context']['decision_ts'] and rec['t0']==e['context']['decision_ts']
    assert rec['recovery_source_provenance']=={'snapshots':'unrecorded','narrative_tags':'unrecorded'}


def test_rebuilt_economic_fields_match_normal_commit_builder(account,tmp_path,monkeypatch):
    from types import SimpleNamespace
    a,store,state,scanner,cfg=setup(tmp_path);path=tmp_path/'history.jsonl';cfg.degen.backtest_dataset_path=str(path)
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**kw:None)
    scanner._manage_exits(cfg,a,state)
    e=store.journal_query(kind='scanner_close_committed')[0]['detail'];rebuilt=close_outbox.rebuild_backtest(e)['record']
    c=e['context'];r=e['native_receipt'];pos=dict(c['position']);entry=r['allocations'][0]['entry_price_sol']
    # Normal preclose builder receives decision time, then commit patches only
    # native outcome fields. Exercise that actual commit with the built spec.
    normal=LaunchScanner.__new__(LaunchScanner)._backtest_record_paper_close(cfg,state,pos['mint'],pos['symbol'],pos,entry,0,c['reason'],0,c['decision_ts'],record_only=True,snapshots_override=[])
    normal_context=dict(c,backtest_spec=normal)
    class Capture:
        def scanner_close_event(self,event,evidence):self.evidence=evidence;return False
    target=Capture();close_outbox.commit(target,normal_context,r)
    expected=target.evidence['backtest_spec']['record']
    for key in ('t0','t_entry','t_close','entry_price_sol','close_price_sol','peak_mult','realized_mult','pnl_sol','hold_min','outcome_id','outcome','native_allocation','native_allocation_state','realized_mult_metric','snapshot_input_evidence'):
        assert rebuilt[key]==expected[key],key


import pytest,copy
@pytest.mark.parametrize('mutation',['future','negative','count','tags','provenance','native'])
def test_invalid_pinned_builder_source_never_writes(account,tmp_path,monkeypatch,mutation):
    a,store,state,scanner,cfg=setup(tmp_path);path=tmp_path/'history.jsonl';cfg.degen.backtest_dataset_path=str(path)
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**kw:None)
    with monkeypatch.context() as m:
        m.setattr(close_outbox,'rebuild_backtest',lambda *a:None)
        scanner._manage_exits(cfg,a,state)
    rows=copy.deepcopy(store.journal_query(kind='scanner_close_committed'));e=rows[0]['detail'];source=e['context']['backtest_source']
    snap={'t':1,'mcap_usd':1,'price_usd':1,'vol5m_usd':1,'buys5m':1,'sells5m':1,'liq_usd':1}
    if mutation in ('future','negative','count'):
        source.update(snapshots=[snap],snapshots_state='recorded')
        if mutation=='future':snap['t']=e['context']['decision_ts']+1
        elif mutation=='negative':snap['liq_usd']=-1
        else:snap['buys5m']=1.5
    elif mutation=='tags':source['narrative_tags']=None
    elif mutation=='provenance':source['snapshots_state']='unknown'
    else:e['native_receipt']['realized_pnl_sol']=999
    original=store.journal_query
    monkeypatch.setattr(store,'journal_query',lambda **kw:rows if kw.get('kind')=='scanner_close_committed' else original(**kw))
    close_outbox.project_backtests(store)
    assert not path.exists() and not original(kind='scanner_backtest_projected')


def test_actual_fingerprint_observe_integral_float_counts_capture(tmp_path):
    from daemon.launch_scanner.fingerprints import FingerprintTracker
    from types import SimpleNamespace
    tracker=FingerprintTracker(SimpleNamespace(fp_series_journal_enabled=False))
    tracker.observe('m',{'usd_market_cap':100,'price_usd':1,'vol_m5':5,'buys_m5':1,'sells_m5':0,'usd_liquidity':10},10)
    scanner=LaunchScanner.__new__(LaunchScanner);scanner._fp_tracker=tracker
    source=close_outbox.backtest_source(scanner,SimpleNamespace(backtest_dataset_path=str(tmp_path/'history.jsonl')),{},'m',11)
    assert source['state']=='recorded'
    assert type(source['snapshots'][0]['buys5m']) is int and source['snapshots'][0]['buys5m']==1
    assert type(source['snapshots'][0]['sells5m']) is int


def test_rebuilt_dataset_corruption_pending_then_retries(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path);path=tmp_path/'history.jsonl';cfg.degen.backtest_dataset_path=str(path)
    path.write_text('{bad\n')
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**kw:None)
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad\n'
    assert not store.journal_query(kind='scanner_backtest_projected')
    path.unlink();close_outbox.project_backtests(store);close_outbox.project_backtests(store)
    assert len(path.read_text().splitlines())==1


def test_fresh_process_rebuild_write_then_death_before_ack(account,tmp_path,monkeypatch):
    import subprocess,sys,os,textwrap
    a,store,state,scanner,cfg=setup(tmp_path);path=tmp_path/'history.jsonl';cfg.degen.backtest_dataset_path=str(path)
    monkeypatch.setattr(scanner,'_backtest_record_paper_close',lambda *a,**kw:None)
    with monkeypatch.context() as m:
        m.setattr(close_outbox,'rebuild_backtest',lambda *a:None)
        scanner._manage_exits(cfg,a,state)
    assert not path.exists();store.close()
    code='''
import os,sys
from storage.store import Store
from daemon.launch_scanner import close_outbox,backtest
store=Store(sys.argv[1])
if sys.argv[2]=='crash':
    original=backtest.record_history
    def crash(*args):
        assert original(*args)
        os._exit(39)
    backtest.record_history=crash
close_outbox.project_backtests(store)
assert len(store.journal_query(kind='scanner_backtest_projected'))==1
'''
    args=[sys.executable,'-c',textwrap.dedent(code),str(tmp_path/'scanner.db')]
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    first=subprocess.run(args+['crash'],env=env,capture_output=True,text=True)
    assert first.returncode==39,first.stderr
    assert len(path.read_text().splitlines())==1
    for _ in range(2):
        retry=subprocess.run(args+['recover'],env=env,capture_output=True,text=True)
        assert retry.returncode==0,retry.stderr
        assert len(path.read_text().splitlines())==1


def test_canonical_snapshot_path_follows_runtime_home(tmp_path,monkeypatch):
    from config.config import DegenConfig
    monkeypatch.setenv('FOREX_AGENT_HOME',str(tmp_path))
    monkeypatch.delenv('DEGEN_FP_SERIES_JOURNAL_PATH',raising=False)
    assert DegenConfig().fp_series_journal_path==str(tmp_path/'launch_scanner'/'fp_series_journal.jsonl')
    monkeypatch.setenv('DEGEN_FP_SERIES_JOURNAL_PATH',str(tmp_path/'explicit.jsonl'))
    assert DegenConfig().fp_series_journal_path==str(tmp_path/'explicit.jsonl')


def test_fresh_close_snapshot_fallback_is_mint_bound_and_not_complete(tmp_path):
    from types import SimpleNamespace
    from daemon.launch_scanner.fingerprints import FingerprintTracker
    path=tmp_path/'series.jsonl'
    cfg=SimpleNamespace(fp_series_journal_path=str(path))
    tr=FingerprintTracker(cfg)
    tr.observe('M',{'usd_market_cap':10,'price_usd':1},100.)
    tr.observe('other',{'usd_market_cap':99,'price_usd':2},101.)
    fresh=LaunchScanner.__new__(LaunchScanner)
    assert close_outbox.snapshot_series(fresh,cfg,'M',200.,since=99.)==[(100.,10.,1.,0.,0.,0.,0.,None)]
    assert close_outbox.snapshot_series(fresh,cfg,'missing',200.,since=99.) is None
    assert close_outbox.snapshot_series(fresh,SimpleNamespace(), 'M',200.) is None
    # Corrupt same-mint numeric input must not become a plausible zero.
    with path.open('a') as f:
        f.write(json.dumps({'mint':'M','obs':[110.,1.,1.,1.,'bad',0.,1.,None]})+'\n')
    with pytest.raises(ValueError):close_outbox.snapshot_series(fresh,cfg,'M',200.,since=99.)


def test_snapshot_episode_bounds_and_compaction_are_explicit(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from daemon.launch_scanner.fingerprints import FingerprintTracker
    path=tmp_path/'series.jsonl';cfg=SimpleNamespace(fp_series_journal_path=str(path))
    tr=FingerprintTracker(cfg)
    for ts in (100.,200.,300.):tr.observe('M',{'price_usd':1.},ts)
    fresh=LaunchScanner.__new__(LaunchScanner)
    assert [x[0] for x in close_outbox.snapshot_series(fresh,cfg,'M',250.,since=150.)]==[200.]
    import daemon.launch_scanner.fingerprints as fp
    monkeypatch.setattr(fp,'_COMPACT_MIN_BYTES',0)
    tr._journal_compact(30*3600.)
    assert close_outbox.snapshot_series(fresh,cfg,'M',30*3600.,since=150.) is None


def test_corrupt_journal_cannot_bypass_pinned_source_via_normal_builder(account,tmp_path,monkeypatch):
    import time
    a,store,state,scanner,cfg=setup(tmp_path)
    mint=state['degen_positions']['M']['mint'];now=time.time()
    journal=tmp_path/'series.jsonl';history=tmp_path/'history.jsonl'
    journal.write_text(json.dumps({'mint':mint,'obs':[now-1,1.,1.,1.,'bad',0.,1.,None]})+'\n')
    cfg.degen.fp_series_journal_path=str(journal)
    cfg.degen.backtest_dataset_path=str(history)
    state['seen']={mint:{'first_ts':now-2}}
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and not a.positions()
    evidence=store.journal_query(kind='scanner_close_committed')[0]['detail']
    assert evidence['context']['backtest_source']['snapshots_state']=='unavailable'
    record=json.loads(history.read_text())
    assert record['snapshots']==[] and record['snapshot_input_evidence']['state']=='invalid-observation'
    assert record['native_allocation']['realized_pnl_sol']==evidence['native_receipt']['realized_pnl_sol']
    assert len(store.journal_query(kind='scanner_backtest_projected'))==1
    assert len(store.journal_query(kind='scanner_close_committed'))==1
