import pytest
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,close_outbox,exit_liquidity_clock as clock
from daemon.common import get_daemon_state


def test_actual_close_crash_uses_pinned_clock_path_and_peak(account,tmp_path,monkeypatch):
    path=tmp_path/'clock.json';monkeypatch.setattr(clock,'_resolve_path',lambda p:str(path))
    a,store,state,scanner,cfg=setup(tmp_path)
    mint=state['degen_positions']['M']['mint'];clock._PEAKS[mint]=[.000002,state['degen_positions']['M']['entry_ts']+100]
    original=a.close_position
    def crash(ticket):original(ticket);raise SystemExit('native close committed')
    monkeypatch.setattr(a,'close_position',crash)
    with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
    assert not path.exists()
    clock._PEAKS.clear();monkeypatch.setattr(clock,'_resolve_path',lambda p:str(tmp_path/'wrong.json'))
    cfg.degen.exit_clock_enabled=False
    fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
    fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    records=clock._all_records(str(path));assert len(records)==1
    assert records[0]['time_to_peak_h']==round(100/3600,4)
    assert not (tmp_path/'wrong.json').exists()
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_exit_clock_projected'");store._conn.commit()
    close_outbox.project_exit_clock(store)
    assert clock._all_records(str(path))==records


def test_clock_corruption_does_not_block_exit_then_retries(account,tmp_path,monkeypatch):
    path=tmp_path/'clock.json';path.write_text('{bad');monkeypatch.setattr(clock,'_resolve_path',lambda p:str(path))
    a,store,state,scanner,cfg=setup(tmp_path)
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad'
    assert not store.journal_query(kind='scanner_exit_clock_projected')
    path.unlink();close_outbox.project_exit_clock(store)
    assert len(clock._all_records(str(path)))==1
    # Current quote prepass legitimately observes a peak before close.
    assert clock._all_records(str(path))[0]['time_to_peak_h'] is not None


@pytest.mark.parametrize('mutation',['native','position','mint','pnl','price','path','peak','enable','ticket'])
def test_forged_clock_source_never_writes(account,tmp_path,monkeypatch,mutation):
    import copy
    path=tmp_path/'clock.json';monkeypatch.setattr(clock,'_resolve_path',lambda p:str(path))
    a,store,state,scanner,cfg=setup(tmp_path)
    with monkeypatch.context() as patcher:
        patcher.setattr(clock,'append_record',lambda *a:(_ for _ in ()).throw(OSError('defer')))
        scanner._manage_exits(cfg,a,state)
    rows=copy.deepcopy(store.journal_query(kind='scanner_close_committed'));e=rows[0]['detail']
    if mutation=='native':e.pop('native_receipt')
    elif mutation=='position':e['context'].pop('position')
    elif mutation=='mint':e['clock_spec']['record']['mint']='wrong'
    elif mutation=='pnl':e['clock_spec']['record']['pnl_sol']=999
    elif mutation=='price':e['native_receipt']['price_sol']=float('nan')
    elif mutation=='path':e['clock_spec']['path']=str(tmp_path/'wrong.json')
    elif mutation=='peak':e['clock_spec']['peak_info']=[99,999]
    elif mutation=='enable':e['context']['clock_spec']['state']='disabled'
    else:e['native_receipt']['allocations'][0]['ticket']=-1
    original=store.journal_query
    monkeypatch.setattr(store,'journal_query',lambda **kw:rows if kw.get('kind')=='scanner_close_committed' else original(**kw))
    close_outbox.project_exit_clock(store)
    assert not path.exists() and not (tmp_path/'wrong.json').exists()
    assert not original(kind='scanner_exit_clock_projected')
