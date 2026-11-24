"""Shared native orphan economic evidence; missing scanner learning stays missing."""
import time
import pytest
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,scoreboard


def test_orphan_native_close_before_event_must_recover(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path)
    state['degen_positions']={}
    native=a._positions['1'];native['time_open']='2026-11-01T00:00:00+00:00';a._save_state()
    original=a.close_position
    def crash(ticket):original(ticket);raise SystemExit('orphan native economic commit')
    monkeypatch.setattr(a,'close_position',crash)
    with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
    assert len(account.snapshot()['pending_sol'])==1
    fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
    for _ in range(2):fresh._manage_exits(cfg,a,{'degen_positions':{}})
    assert len(scoreboard.load_degen_closes(str(store.path)))==1


def test_normal_orphan_close_is_atomic_economic_only(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path);state['degen_positions']={}
    a._positions['1']['time_open']='2026-11-01T00:00:00+00:00';a._save_state()
    scanner._manage_exits(cfg,a,state)
    rows=store.journal_query(kind='scanner_close_committed');assert len(rows)==1
    e=rows[0]['detail'];assert e['context']['orphan'] is True
    assert e['context']['source_provenance']['scanner_position']=='unavailable'
    for key in ('backtest_source','wallet_learning_spec','clock_spec','narrative_spec','exit_copy_spec','deployer_spec'):
        assert e['context'][key]=={'state':'unavailable-orphan-scanner-context'}
    assert e['native_receipt']['realized_pnl_sol']==0
    assert e['backtest_spec'] is None
    for kind in ('backtest','wallet_learning','exit_clock','narrative','exit_copy','deployer'):
        assert store.journal_query(kind='scanner_'+kind+'_projected')==[]
    assert len(scoreboard.load_degen_closes(str(store.path)))==1


def test_orphan_context_storage_failure_prevents_unrecoverable_close(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path);state['degen_positions']={}
    a._positions['1']['time_open']='2026-11-01T00:00:00+00:00';a._save_state()
    monkeypatch.setattr(store,'scanner_close_context',lambda *a:(_ for _ in ()).throw(OSError('storage unavailable')))
    scanner._manage_exits(cfg,a,state)
    assert len(a.positions())==1 and account.snapshot()['pending_sol']==[]


def test_other_book_orphan_intent_does_not_block_this_book(account,tmp_path,monkeypatch):
    from daemon.launch_scanner import close_outbox
    a,store,state,scanner,cfg=setup(tmp_path)
    identity=dict(a.scanner_close_identity(1),book_id='anotherbook')
    close_outbox.prepare_orphan(store,identity,{'symbol':'BONK','ticket':1},'max_hold',time.time())
    close_outbox.recover_orphans(store,a)
    assert len(a.positions())==1 and not store.journal_query(kind='scanner_close_committed')


def test_actual_orphan_child_death_then_two_fresh_replays(account,tmp_path):
    import os,subprocess,sys
    a,store,state,scanner,cfg=setup(tmp_path)
    a._positions['1']['time_open']='2026-11-01T00:00:00+00:00';a._save_state();store.close()
    source='''
import os,sys,time
from types import SimpleNamespace
from daemon.launch_scanner import LaunchScanner,paper_account as pa,scoreboard
from storage.store import Store
from tests.test_broker_memecoin import _adapter,FakeMarket
folder,native,db,mode=sys.argv[1:]
pa.ACCOUNT_DIR=folder;pa.LEDGER_PATH=folder+'/ledger.json';pa.JOURNAL_PATH=folder+'/events.jsonl';pa.LOCK_FILE=folder+'/.lock'
a=_adapter(native,FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
a.connect({});store=Store(db);scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=store
cfg=SimpleNamespace(degen=SimpleNamespace(max_hold_hours=4.,entry_sol=.02))
if mode=='crash':
 original=a.close_position
 def crash(ticket):
  original(ticket);os._exit(41)
 a.close_position=crash
scanner._manage_exits(cfg,a,{'degen_positions':{}})
assert len(scoreboard.load_degen_closes(db))==1
assert len(store.journal_query(kind='scanner_close_committed'))==1
assert len(pa.snapshot()['pending_sol'])==1
assert not a.positions()
'''
    args=[sys.executable,'-c',source,account.ACCOUNT_DIR,str(tmp_path/'native'),str(tmp_path/'scanner.db')]
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    first=subprocess.run(args+['crash'],env=env,capture_output=True,text=True)
    assert first.returncode==41,first.stderr
    for _ in range(2):
        replay=subprocess.run(args+['recover'],env=env,capture_output=True,text=True)
        assert replay.returncode==0,replay.stderr


def test_orphan_event_storage_failure_retries_from_native_receipt(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path);state['degen_positions']={}
    a._positions['1']['time_open']='2026-11-01T00:00:00+00:00';a._save_state()
    original=store.scanner_close_event
    monkeypatch.setattr(store,'scanner_close_event',lambda *a:(_ for _ in ()).throw(OSError('projection unavailable')))
    scanner._manage_exits(cfg,a,state)
    assert not a.positions() and not store.journal_query(kind='scanner_close_committed')
    monkeypatch.setattr(store,'scanner_close_event',original)
    monkeypatch.setattr(a,'_price_sol_quiet',lambda *a:(_ for _ in ()).throw(AssertionError('sold quote not needed')))
    scanner._manage_exits(cfg,a,state);scanner._manage_exits(cfg,a,state)
    assert len(store.journal_query(kind='scanner_close_committed'))==1


def test_pending_orphan_poolgone_not_reused_after_quote_returns(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path);state['degen_positions']={}
    a._positions['1']['time_open']='2026-11-01T00:00:00+00:00';a._save_state()
    original=a.close_position_at
    monkeypatch.setattr(a,'_price_sol_quiet',lambda symbol:None if symbol=='BONK' else 1.)
    monkeypatch.setattr(a,'close_position_at',lambda *a,**kw:(_ for _ in ()).throw(OSError('submit unavailable')))
    scanner._manage_exits(cfg,a,state)
    assert store.journal_query(kind='scanner_close_intent')[0]['detail']['reason']=='pool_gone'
    monkeypatch.setattr(a,'_price_sol_quiet',lambda symbol:.000001)
    monkeypatch.setattr(a,'close_position_at',original)
    scanner._manage_exits(cfg,a,state)
    assert len(a.positions())==1 and account.snapshot()['pending_sol']==[]


@pytest.mark.parametrize('failure',['receipt','malformed','event'])
def test_bad_orphan_cannot_block_tracked_exit_or_second_orphan(account,tmp_path,monkeypatch,failure):
    from daemon.launch_scanner import close_outbox
    from tests.test_broker_memecoin import _req
    a,store,state,scanner,cfg=setup(tmp_path)
    good=a.scanner_close_identity(1)
    close_outbox.prepare_orphan(store,good,{'ticket':1,'symbol':'BONK'},'max_hold',time.time())
    a.close_position(1)
    bad=dict(good,ticket=999)
    close_outbox.prepare_orphan(store,bad,{'ticket':999,'symbol':'BONK'},'max_hold',time.time())
    if failure=='malformed':
        with store._conn:
            row=store._conn.execute("SELECT id,payload FROM journal WHERE kind='scanner_close_intent' AND symbol=?",(close_outbox.key(bad),)).fetchone()
            import json
            payload=json.loads(row['payload']);payload['detail']['identity']=None
            store._conn.execute('UPDATE journal SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
    else:
        original=a.scanner_close_receipt
        def receipt(identity):
            if identity['ticket']==999:
                if failure=='receipt':raise ValueError('invalid receipt')
                return dict(original(good),ticket=999,allocations=[dict(original(good)['allocations'][0],ticket=999)])
            return original(identity)
        monkeypatch.setattr(a,'scanner_close_receipt',receipt)
        if failure=='event':
            commit=store.scanner_close_event
            def event(ev,evidence):
                if ev['ticket']==999:raise OSError('orphan scoreboard unavailable')
                return commit(ev,evidence)
            monkeypatch.setattr(store,'scanner_close_event',event)
    a.submit_order(_req('BONK','BUY',.02,'second'))
    state['degen_positions']['M']['ticket']=2
    scanner._manage_exits(cfg,a,state)
    assert not a.positions() and not state['degen_positions']
    assert {e['detail']['native_receipt']['ticket'] for e in store.journal_query(kind='scanner_close_committed')}=={1,2}
    assert len(account.snapshot()['pending_sol'])==2
    assert close_outbox.load(store,bad) is not None if failure!='malformed' else True


def test_orphan_source_scan_failure_does_not_block_tracked_exit(account,tmp_path,monkeypatch):
    a,store,state,scanner,cfg=setup(tmp_path)
    original=store.journal_query
    def query(**kw):
        if kw.get('kind')=='scanner_close_intent' and kw.get('limit')==-1:raise OSError('scan unavailable')
        return original(**kw)
    monkeypatch.setattr(store,'journal_query',query)
    scanner._manage_exits(cfg,a,state)
    assert not a.positions() and len(original(kind='scanner_close_committed'))==1
