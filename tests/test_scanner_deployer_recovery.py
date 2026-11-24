import pytest
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,close_outbox,deployers as clock
from daemon.common import get_daemon_state


def test_actual_close_crash_uses_pinned_copy_path(account,tmp_path,monkeypatch):
    path=tmp_path/'copy.json';monkeypatch.setattr(clock,'_REGISTRY_FILE',path)
    a,store,state,scanner,cfg=setup(tmp_path)
    state['degen_positions']['M']['deployer']='dep'
    mint=state['degen_positions']['M']['mint'];state['degen_positions']['M']['deployer']='dep'
    original=a.close_position
    def crash(ticket):original(ticket);raise SystemExit('native close committed')
    monkeypatch.setattr(a,'close_position',crash)
    with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
    assert not path.exists()
    monkeypatch.setattr(clock,'_REGISTRY_FILE',tmp_path/'wrong.json')
    cfg.degen.deployer_enabled=False
    fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
    fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    records=list(__import__('daemon.launch_scanner.deployer_registry',fromlist=['read']).read(path).values());assert len(records)==1
    assert not (tmp_path/'wrong.json').exists()
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_deployer_projected'");store._conn.commit()
    close_outbox.project_deployers(store)
    assert list(__import__('daemon.launch_scanner.deployer_registry',fromlist=['read']).read(path).values())==records


def test_copy_corruption_does_not_block_exit_then_retries(account,tmp_path,monkeypatch):
    path=tmp_path/'copy.json';path.write_text('{bad');monkeypatch.setattr(clock,'_REGISTRY_FILE',path)
    a,store,state,scanner,cfg=setup(tmp_path)
    state['degen_positions']['M']['deployer']='dep'
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad'
    assert not store.journal_query(kind='scanner_deployer_projected')
    path.unlink();close_outbox.project_deployers(store)
    assert len(list(__import__('daemon.launch_scanner.deployer_registry',fromlist=['read']).read(path).values()))==1


@pytest.mark.parametrize('mutation',['native','position','mint','pnl','price','path','entry','enable','ticket'])
def test_forged_copy_source_never_writes(account,tmp_path,monkeypatch,mutation):
    import copy
    path=tmp_path/'copy.json';monkeypatch.setattr(clock,'_REGISTRY_FILE',path)
    a,store,state,scanner,cfg=setup(tmp_path)
    state['degen_positions']['M']['deployer']='dep'
    with monkeypatch.context() as patcher:
        patcher.setattr(clock,'claim_outcome',lambda *a:(_ for _ in ()).throw(OSError('defer')))
        scanner._manage_exits(cfg,a,state)
    rows=copy.deepcopy(store.journal_query(kind='scanner_close_committed'));e=rows[0]['detail']
    if mutation=='native':e.pop('native_receipt')
    elif mutation=='position':e['context'].pop('position')
    elif mutation=='mint':e['deployer_spec']['mint']='wrong'
    elif mutation=='pnl':e['native_receipt']['realized_pnl_sol']=999
    elif mutation=='price':e['native_receipt']['price_sol']=float('nan')
    elif mutation=='path':e['deployer_spec']['path']=str(tmp_path/'wrong.json')
    elif mutation=='entry':e['deployer_spec']['observed_at']=999
    elif mutation=='enable':e['context']['deployer_spec']['state']='disabled'
    else:e['native_receipt']['allocations'][0]['ticket']=-1
    original=store.journal_query
    monkeypatch.setattr(store,'journal_query',lambda **kw:rows if kw.get('kind')=='scanner_close_committed' else original(**kw))
    close_outbox.project_deployers(store)
    assert not path.exists() and not (tmp_path/'wrong.json').exists()
    assert not original(kind='scanner_deployer_projected')




def test_fresh_process_death_after_durable_append_replays_once(account,tmp_path,monkeypatch):
    import os,subprocess,sys,textwrap
    path=tmp_path/'deployer.json';monkeypatch.setattr(clock,'_REGISTRY_FILE',path)
    a,store,state,scanner,cfg=setup(tmp_path)
    state['degen_positions']['M']['deployer']='dep'
    with monkeypatch.context() as m:
        m.setattr(clock,'claim_outcome',lambda *a:(_ for _ in ()).throw(OSError('defer to worker')))
        scanner._manage_exits(cfg,a,state)
    assert not path.exists();store.close()
    script='''
import os,sys
from storage.store import Store
from daemon.launch_scanner import close_outbox,deployers as clock
store=Store(sys.argv[1])
if sys.argv[2]=='crash':
    original=clock.claim_outcome
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        os._exit(37)
    clock.claim_outcome=crash
close_outbox.project_deployers(store)
assert len(store.journal_query(kind='scanner_deployer_projected'))==1
'''
    args=[sys.executable,'-c',textwrap.dedent(script),str(tmp_path/'scanner.db')]
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    first=subprocess.run(args+['crash'],env=env,capture_output=True,text=True)
    assert first.returncode==37,first.stderr
    assert len(__import__('daemon.launch_scanner.deployer_registry',fromlist=['read']).read(path))==1
    for _ in range(2):
        result=subprocess.run(args+['recover'],env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert len(__import__('daemon.launch_scanner.deployer_registry',fromlist=['read']).read(path))==1
