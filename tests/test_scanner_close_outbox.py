"""Offline actual shared/native close recovery and durable event projection."""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from daemon.launch_scanner import LaunchScanner, close_outbox, scoreboard
from daemon.common import save_daemon_state, get_daemon_state
from storage.store import Store
from tests.test_broker_memecoin import _adapter, FakeMarket, _req
from tests.test_paper_account_instances import account
from daemon.launch_scanner import paper_account as pa


def setup(tmp_path):
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});a.submit_order(_req('BONK','BUY',.02,'entry'))
    pos={'ticket':1,'symbol':'BONK','mint':a._market.mint_for('BONK'),
        'entry_ts':time.time()-30*3600,'entry_price_sol':.000001,'volume_sol':.02,
        'sl':.0000007,'tp':.000003,'max_hold_hours':4.,'leg':'A'}
    store=Store(tmp_path/'scanner.db');state={'degen_positions':{'M':pos}}
    save_daemon_state(store,'launch_scanner',state)
    scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=store
    cfg=SimpleNamespace(degen=SimpleNamespace(max_hold_hours=4.,entry_sol=.02))
    return a,store,state,scanner,cfg



class TestScannerCloseOutbox:

    def test_restart_after_economic_close_recovers_once_without_quote(self,account,tmp_path,monkeypatch):
        a,store,state,scanner,cfg=setup(tmp_path)
        original=a.close_position
        def crash(ticket):
            original(ticket)
            raise SystemExit('economic commit before scanner event')
        monkeypatch.setattr(a,'close_position',crash)
        with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
        assert len(pa.snapshot()['pending_sol'])==1
        assert scoreboard.load_degen_closes(str(store.path))==[]
        store.close();store=Store(tmp_path/'scanner.db')
        b=_adapter(str(tmp_path/'native'),FakeMarket({}),shared_account=pa,
            fx_observer=lambda:(_ for _ in ()).throw(ValueError('no FX')))
        b.connect({})
        fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
        loaded=get_daemon_state(store,'launch_scanner')
        fresh._manage_exits(cfg,b,loaded)
        assert loaded['degen_positions']=={}
        assert len(scoreboard.load_degen_closes(str(store.path)))==1
        # Durable stale scanner snapshot can replay repeatedly, even if queue consumed.
        fresh._manage_exits(cfg,b,get_daemon_state(store,'launch_scanner'))
        assert len(scoreboard.load_degen_closes(str(store.path)))==1
        store.dequeue_events(100)
        fresh._manage_exits(cfg,b,get_daemon_state(store,'launch_scanner'))
        assert len(scoreboard.load_degen_closes(str(store.path)))==1
        assert len(store.journal_query(kind='scanner_close_committed'))==1
        assert len(pa.snapshot()['pending_sol'])==1


    def test_context_write_failure_precedes_economic_effect(self,account,tmp_path,monkeypatch):
        a,store,state,scanner,cfg=setup(tmp_path)
        monkeypatch.setattr(store,'scanner_close_context',lambda *a:(_ for _ in ()).throw(OSError('disk')))
        with pytest.raises(OSError):scanner._manage_exits(cfg,a,state)
        assert len(a.positions())==1 and pa.snapshot()['pending_sol']==[]


    def test_atomic_projection_rollback_then_exact_retry(self,account,tmp_path):
        a,store,state,scanner,cfg=setup(tmp_path)
        pos=state['degen_positions']['M'];identity=a.scanner_close_identity(1)
        context=close_outbox.prepare(store,identity,pos,'M','max_hold',.000001,time.time(),state,.02)
        a.close_position(1);receipt=a.scanner_close_receipt(identity)
        store._conn.execute("CREATE TRIGGER fail_close BEFORE INSERT ON event_journal BEGIN SELECT RAISE(ABORT,'fault'); END")
        store._conn.commit()
        with pytest.raises(Exception,match='fault'):close_outbox.commit(store,context,receipt)
        assert scoreboard.load_degen_closes(str(store.path))==[]
        assert store.journal_query(kind='scanner_close_committed')==[]
        store._conn.execute('DROP TRIGGER fail_close');store._conn.commit()
        close_outbox.commit(store,context,receipt);close_outbox.commit(store,context,receipt)
        assert len(scoreboard.load_degen_closes(str(store.path)))==1
        bad=dict(receipt,price_sol=.000002)
        with pytest.raises(ValueError,match='economics conflict'):close_outbox.commit(store,context,bad)


    def test_fresh_interpreter_crash_and_recovery(self,account,tmp_path):
        import os,subprocess,sys,textwrap
        a,store,state,scanner,cfg=setup(tmp_path)
        folder=pa.ACCOUNT_DIR;store.close()
        source='''
    import os,sys,time
    from types import SimpleNamespace
    from daemon.launch_scanner import LaunchScanner,paper_account as pa,close_outbox,scoreboard
    from daemon.common import get_daemon_state
    from storage.store import Store
    from tests.test_broker_memecoin import _adapter,FakeMarket
    folder,native,db,mode=sys.argv[1:]
    pa.ACCOUNT_DIR=folder;pa.LEDGER_PATH=folder+'/ledger.json';pa.JOURNAL_PATH=folder+'/events.jsonl';pa.LOCK_FILE=folder+'/.lock'
    a=_adapter(native,FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(db);scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=store
    state=get_daemon_state(store,'launch_scanner');cfg=SimpleNamespace(degen=SimpleNamespace(max_hold_hours=4.,entry_sol=.02))
    if mode=='crash':
     original=a.close_position
     def crash(ticket):
      original(ticket);os._exit(29)
     a.close_position=crash
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions']
    assert len(scoreboard.load_degen_closes(db))==1
    assert len(pa.snapshot()['pending_sol'])==1
    '''
        args=[sys.executable,'-c',textwrap.dedent(source),folder,str(tmp_path/'native'),str(tmp_path/'scanner.db')]
        env=dict(os.environ,FOREX_AGENT_TESTING='1')
        first=subprocess.run(args+['crash'],env=env,capture_output=True,text=True)
        assert first.returncode==29,first.stderr
        for _ in range(2):
            result=subprocess.run(args+['recover'],env=env,capture_output=True,text=True)
            assert result.returncode==0,result.stderr


    def test_uncommitted_intent_keeps_original_exit_reason(self,account,tmp_path,monkeypatch):
        a,store,state,scanner,cfg=setup(tmp_path)
        original=a.close_position
        monkeypatch.setattr(a,'close_position',lambda ticket:(_ for _ in ()).throw(ValueError('quote outage')))
        scanner._manage_exits(cfg,a,state)
        assert len(a.positions())==1
        # New quote crosses a different exit trigger. Retry still belongs to max_hold decision.
        a._market.usd['BONK']=.001
        monkeypatch.setattr(a,'close_position',original)
        scanner._manage_exits(cfg,a,state)
        rows=store.journal_query(kind='event')
        assert rows[0]['payload']['reason']=='degen_max_hold'


    def test_stale_scanner_basis_never_changes_native_loss(self,account,tmp_path):
        a,store,state,scanner,cfg=setup(tmp_path)
        state['degen_positions']['M']['volume_sol']=2.
        a._market.usd['BONK']=.00005
        with patch.object(scanner,'_grade_deployer_close'),patch.object(scanner,'_record_narrative_close'),patch.object(scanner,'_backtest_record_paper_close'):
            scanner._manage_exits(cfg,a,state)
        assert scoreboard.load_degen_closes(str(store.path))[0]['profit']==-.01
        evidence=store.journal_query(kind='scanner_close_committed')[0]['detail']
        assert evidence['native_receipt']['realized_pnl_sol']==pytest.approx(-.01)
        assert evidence['context']['position']['volume_sol']==2.


    def test_committed_recovery_does_not_quote_sold_token(self,account,tmp_path,monkeypatch):
        a,store,state,scanner,cfg=setup(tmp_path)
        pos=state['degen_positions']['M'];identity=a.scanner_close_identity(1)
        close_outbox.prepare(store,identity,pos,'M','max_hold',.000001,time.time(),state,.02)
        a.close_position(1)
        called=[]
        def quote(symbol):
            called.append(symbol)
            raise AssertionError('recovery must precede quote')
        monkeypatch.setattr(a,'_price_sol_quiet',quote)
        scanner._manage_exits(cfg,a,state)
        assert called==[]
        assert not state['degen_positions']
        assert len(scoreboard.load_degen_closes(str(store.path)))==1


    def test_scoreboard_legacy_queue_only_database(self,tmp_path):
        import sqlite3
        path=tmp_path/'old.db';db=sqlite3.connect(path)
        db.execute('CREATE TABLE event_queue(ts TEXT,event TEXT)')
        for ts,event in [('2026-01-01T00:00:00Z',{'event':'position.closed','reason':'degen_stop_loss','profit':-.02}),
                         ('2026-01-02T00:00:00Z',{'event':'position.closed','reason':'other','profit':1})]:
            db.execute('INSERT INTO event_queue VALUES (?,?)',(ts,json.dumps(event)))
        db.commit();db.close()
        closes=scoreboard.load_degen_closes(str(path))
        assert len(closes)==1 and closes[0]['profit']==-.02


    def test_scoreboard_merges_legacy_and_durable_once(self,account,tmp_path):
        a,store,state,scanner,cfg=setup(tmp_path)
        pos=state['degen_positions']['M'];identity=a.scanner_close_identity(1)
        context=close_outbox.prepare(store,identity,pos,'M','max_hold',.000001,time.time(),state,.02)
        a.close_position(1);close_outbox.commit(store,context,a.scanner_close_receipt(identity))
        store.enqueue_event({'event':'position.closed','event_id':'legacy','reason':'degen_stop_loss','profit':-.03})
        closes=scoreboard.load_degen_closes(str(store.path))
        assert len(closes)==2 and scoreboard.summarize(closes)['total_pnl_sol']==-.03
        store.dequeue_events(100)
        assert len(scoreboard.load_degen_closes(str(store.path)))==1


    def test_receipt_other_book_refused(self,account,tmp_path):
        a,store,state,scanner,cfg=setup(tmp_path)
        identity=a.scanner_close_identity(1);a.close_position(1)
        with pytest.raises(ValueError,match='identity conflict'):
            a.scanner_close_receipt(dict(identity,book_id='another-book'))

    def test_actual_scanner_pinned_backtest_recovers_after_close_crash(self,account,tmp_path,monkeypatch):
        from daemon.launch_scanner import backtest
        a,store,state,scanner,cfg=setup(tmp_path)
        cfg.degen.backtest_dataset_path=str(tmp_path/'history.jsonl')
        a._market.usd['BONK']=.00005
        original=a.close_position
        def crash(ticket):
            original(ticket);raise SystemExit('after actual close')
        monkeypatch.setattr(a,'close_position',crash)
        with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
        assert not (tmp_path/'history.jsonl').exists()
        store.close();fresh=Store(tmp_path/'scanner.db')
        scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=fresh
        # Changing config after decision cannot redirect or disable accepted evidence.
        cfg.degen.backtest_dataset_path=str(tmp_path/'wrong.jsonl')
        cfg.degen.backtest_enabled=False
        monkeypatch.setattr(a,'_price_sol_quiet',lambda symbol:(_ for _ in ()).throw(AssertionError('no quote')))
        loaded=get_daemon_state(fresh,'launch_scanner')
        scanner._manage_exits(cfg,a,loaded)
        rows=[json.loads(line) for line in (tmp_path/'history.jsonl').read_text().splitlines()]
        assert len(rows)==1 and rows[0]['pnl_sol']==-.01
        assert rows[0]['outcome_id']==close_outbox.key(a.scanner_close_identity(1))
        assert not (tmp_path/'wrong.jsonl').exists()
        # Even with no positions, lost projection acknowledgments recover exactly.
        fresh._conn.execute("DELETE FROM journal WHERE kind='scanner_backtest_projected'");fresh._conn.commit()
        scanner._manage_exits(cfg,a,{'degen_positions':{}})
        assert len((tmp_path/'history.jsonl').read_text().splitlines())==1

    def test_backtest_write_failure_keeps_pinned_pending_projection(self,account,tmp_path,monkeypatch):
        from daemon.launch_scanner import backtest
        a,store,state,scanner,cfg=setup(tmp_path)
        cfg.degen.backtest_dataset_path=str(tmp_path/'history.jsonl')
        real=backtest.record_history
        with patch.object(backtest,'record_history',return_value=False),patch.object(scanner,'_grade_deployer_close'),patch.object(scanner,'_record_narrative_close'):
            scanner._manage_exits(cfg,a,state)
        assert not state['degen_positions']
        assert len(store.journal_query(kind='scanner_close_committed'))==1
        assert not store.journal_query(kind='scanner_backtest_projected')
        close_outbox.project_backtests(store)
        assert len((tmp_path/'history.jsonl').read_text().splitlines())==1
        assert len(store.journal_query(kind='scanner_backtest_projected'))==1

    def test_projection_ack_failure_does_not_block_second_economic_exit(self,account,tmp_path,monkeypatch):
        a,store,state,scanner,cfg=setup(tmp_path)
        cfg.degen.backtest_dataset_path=str(tmp_path/'history.jsonl')
        # Commit first close but fail every analytics ACK; never fake success.
        original_add=store.journal_add
        def failing_ack(entry):
            if entry.get('kind')=='scanner_backtest_projected':raise OSError('ACK disk fault')
            return original_add(entry)
        monkeypatch.setattr(store,'journal_add',failing_ack)
        with patch.object(scanner,'_grade_deployer_close'),patch.object(scanner,'_record_narrative_close'):
            scanner._manage_exits(cfg,a,state)
        a.submit_order(_req('BONK','BUY',.02,'entry-two'))
        pos=dict(get_daemon_state(store,'launch_scanner')['degen_positions']['M'],ticket=2)
        state={'degen_positions':{'M2':pos}}
        with patch.object(scanner,'_grade_deployer_close'),patch.object(scanner,'_record_narrative_close'):
            scanner._manage_exits(cfg,a,state)
        assert not a.positions() and len(pa.snapshot()['pending_sol'])==2
        assert len((tmp_path/'history.jsonl').read_text().splitlines())==2
        assert not store.journal_query(kind='scanner_backtest_projected')
        monkeypatch.setattr(store,'journal_add',original_add)
        close_outbox.project_backtests(store)
        assert len((tmp_path/'history.jsonl').read_text().splitlines())==2
        assert len(store.journal_query(kind='scanner_backtest_projected'))==2
