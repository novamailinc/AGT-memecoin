import copy
import pytest
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,close_outbox,narratives
from daemon.common import get_daemon_state


def test_native_close_crash_pins_configured_metric_time_tags_path(account,tmp_path,monkeypatch):
    path=tmp_path/'heat.json';monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    a,store,state,scanner,cfg=setup(tmp_path)
    mint=state['degen_positions']['M']['mint'];state['narrative_tags']={mint:['dog']}
    configured=cfg.degen.entry_sol;original=a.close_position
    def crash(ticket):original(ticket);raise SystemExit('closed')
    monkeypatch.setattr(a,'close_position',crash)
    with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
    assert not path.exists()
    monkeypatch.setattr(narratives,'default_heat_path',lambda:tmp_path/'wrong.json')
    cfg.degen.entry_sol=999
    fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
    fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    evidence=store.journal_query(kind='scanner_close_committed')[0]['detail']
    spec=evidence['narrative_spec'];receipt=evidence['native_receipt']
    assert spec['configured_basis_sol']==configured and spec['tags']==['dog']
    assert spec['outcome'][2]==max(0,1+receipt['realized_pnl_sol']/configured)
    assert spec['outcome'][3] is False
    assert narratives.read_heat(path)['outcomes']==[spec['outcome']]
    assert not (tmp_path/'wrong.json').exists()
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_narrative_projected'");store._conn.commit()
    close_outbox.project_narratives(store)
    assert narratives.read_heat(path)['outcomes']==[spec['outcome']]


def test_corruption_pending_flatbook_retry(account,tmp_path,monkeypatch):
    path=tmp_path/'heat.json';path.write_text('{bad')
    monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    a,store,state,scanner,cfg=setup(tmp_path);scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad'
    assert not store.journal_query(kind='scanner_narrative_projected')
    path.unlink();close_outbox.project_narratives(store)
    assert len(narratives.read_heat(path)['outcomes'])==1


@pytest.mark.parametrize('mutation',['native','ticket','pnl','tags','path','basis','metric','time','rug'])
def test_forged_narrative_source_refused(account,tmp_path,monkeypatch,mutation):
    path=tmp_path/'heat.json';monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    a,store,state,scanner,cfg=setup(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(narratives,'append_heat',lambda *a:(_ for _ in ()).throw(OSError('defer')))
        scanner._manage_exits(cfg,a,state)
    rows=copy.deepcopy(store.journal_query(kind='scanner_close_committed'));e=rows[0]['detail']
    if mutation=='native':e.pop('native_receipt')
    elif mutation=='ticket':e['native_receipt']['ticket']=-1
    elif mutation=='pnl':e['native_receipt']['realized_pnl_sol']=999
    elif mutation=='time':e['narrative_spec']['outcome'][0]+=1
    elif mutation=='rug':e['narrative_spec']['outcome'][3]=True
    elif mutation=='basis':e['narrative_spec']['configured_basis_sol']=999
    elif mutation=='metric':e['narrative_spec']['metric']='peak'
    elif mutation=='tags':e['narrative_spec']['tags']=['wrong']
    else:e['narrative_spec']['path']=str(tmp_path/'wrong.json')
    original=store.journal_query
    monkeypatch.setattr(store,'journal_query',lambda **kw:rows if kw.get('kind')=='scanner_close_committed' else original(**kw))
    close_outbox.project_narratives(store)
    assert not path.exists() and not (tmp_path/'wrong.json').exists()
    assert not original(kind='scanner_narrative_projected')


@pytest.mark.parametrize('basis',[None,-1,float('nan'),float('inf'),'0.05'])
def test_invalid_basis_unavailable_without_guess(basis,tmp_path,monkeypatch):
    monkeypatch.setattr(narratives,'default_heat_path',lambda:tmp_path/'heat.json')
    assert close_outbox.narrative_source({},'mint',basis)=={'state':'unavailable-basis'}
    assert close_outbox.narrative_record({'default_volume':.05},{'realized_pnl_sol':1}) is None


def test_zero_configured_basis_preserves_neutral_not_native_ratio(tmp_path,monkeypatch):
    monkeypatch.setattr(narratives,'default_heat_path',lambda:tmp_path/'heat.json')
    context={'default_volume':0,'narrative_tags':['dog'],
             'narrative_spec':close_outbox.narrative_source({'narrative_tags':{'mint':['dog']}},'mint',0)}
    result=close_outbox.narrative_record(context,{'realized_pnl_sol':99,'ts':'2026-11-23T14:00:00+00:00'})
    assert result['outcome'][1:]==[['dog'],1.,False]


def test_fresh_process_death_after_durable_append_replays_once(account,tmp_path,monkeypatch):
    import os,subprocess,sys,textwrap
    path=tmp_path/'heat.json';monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    a,store,state,scanner,cfg=setup(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(narratives,'append_heat',lambda *a:(_ for _ in ()).throw(OSError('defer to worker')))
        scanner._manage_exits(cfg,a,state)
    assert not path.exists();store.close()
    script='''
import os,sys
from storage.store import Store
from daemon.launch_scanner import close_outbox,narratives
store=Store(sys.argv[1])
if sys.argv[2]=='crash':
    original=narratives.append_heat
    def crash(*args):
        original(*args)
        os._exit(37)
    narratives.append_heat=crash
close_outbox.project_narratives(store)
assert len(store.journal_query(kind='scanner_narrative_projected'))==1
'''
    args=[sys.executable,'-c',textwrap.dedent(script),str(tmp_path/'scanner.db')]
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    first=subprocess.run(args+['crash'],env=env,capture_output=True,text=True)
    assert first.returncode==37,first.stderr
    assert len(narratives.read_heat(path)['outcomes'])==1
    for _ in range(2):
        result=subprocess.run(args+['recover'],env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert len(narratives.read_heat(path)['outcomes'])==1
