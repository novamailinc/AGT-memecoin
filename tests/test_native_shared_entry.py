"""Actual native adapter with real shared account, explicit offline injection only."""
import json
import time
from unittest.mock import patch

import pytest
from broker import BrokerError
from broker.memecoin.adapter import MemecoinAdapter
from tests.test_broker_memecoin import _adapter, _req, FakeMarket
from tests.test_paper_account_instances import account
from daemon.launch_scanner import paper_account as pa


def adapter(tmp_path):
    a=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}),
               shared_account=pa,fx_observer=lambda:{'usd_per_sol':100.,
                 'observed_at':time.time(),'source':'fixture:SOL'})
    a.connect({})
    return a


class TestNativeSharedEntry:
    def test_actual_adapter_two_lots_compete_with_other_lane(self, account, tmp_path):
        a=adapter(tmp_path)
        a.submit_order(_req('BONK','BUY',.02,'a'))
        a.submit_order(_req('BONK','BUY',.02,'b'))
        snap=pa.snapshot()
        assert snap['cash']==6. and snap['cost_exposure']==4.
        assert snap['lanes']['main']['open_count']==2
        assert len(a._positions)==2
        assert len({p['shared_pos_id'] for p in a._positions.values()})==2
        assert pa.try_debit('watcher','N',6.,10.,'other')[0]
        before=a._state_path().read_bytes()
        with pytest.raises(BrokerError,match='shared capital refused'):
            a.submit_order(_req('BONK','BUY',.01,'empty'))
        assert a._state_path().read_bytes()==before
        assert pa.snapshot()['cash']==0.
        assert len(a._fills_path().read_text().splitlines())==2

    @pytest.mark.parametrize('boundary',['shared-wal','receipt','native-state','native-fill'])
    def test_uncertain_commit_recovers_exact_no_reprice(self, account, tmp_path, boundary):
        a=adapter(tmp_path)
        original_save=a._save_state
        original_fill=a._journal_fill
        original_atomic=__import__('broker.memecoin.adapter',fromlist=['x'])._write_json_atomic
        def atomic(path,data):
            if boundary=='receipt' and data.get('shared_receipt'):
                raise OSError('receipt write')
            original_atomic(path,data)
        module=__import__('broker.memecoin.adapter',fromlist=['x'])
        with patch.object(pa,'_save_locked',side_effect=OSError('shared WAL')) if boundary=='shared-wal' else patch.object(a,'_save_state',wraps=original_save) as unused:
            with patch.object(a,'_save_state',side_effect=OSError('native state')) if boundary=='native-state' else patch.object(a,'_journal_fill',wraps=original_fill):
                with patch.object(a,'_journal_fill',side_effect=OSError('native fill')) if boundary=='native-fill' else patch.object(module,'_write_json_atomic',side_effect=atomic):
                    with pytest.raises(OSError):
                        a.submit_order(_req('BONK','BUY',.02,'a'))
        pending=json.loads(a._intent_path().read_text())
        plan=pending['shared_entry']
        # Startup recovery must not obtain another FX quote.
        b=_adapter(str(tmp_path),FakeMarket({'SOL':500.,'BONK':1.}),shared_account=pa,
                    fx_observer=lambda: (_ for _ in ()).throw(AssertionError('refetched FX')))
        b.connect({})
        assert pa.snapshot()['cash']==8.
        assert b._positions['1']['shared_entry_fx']==plan['fx']
        assert b._positions['1']['tokens']==plan['qty']
        assert len(b._fills_path().read_text().splitlines())==1
        assert b.submit_order(_req('BONK','BUY',.02,'a')).ticket==1
        assert pa.snapshot()['cash']==8.

    def test_unbound_legacy_does_not_get_imported(self, account, tmp_path):
        old=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}))
        old.connect({}); old.submit_order(_req('BONK','BUY',1.,'legacy'))
        a=adapter(tmp_path)
        with pytest.raises(BrokerError,match='unbound legacy'):
            a.submit_order(_req('BONK','BUY',.01,'new'))
        assert pa.snapshot()['cash']==10.

    def test_actual_fifo_exit_pending_fx_then_other_trade_then_dated_settle(self, account, tmp_path):
        a=adapter(tmp_path)
        a.submit_order(_req('BONK','BUY',.02,'a'))
        a.submit_order(_req('BONK','BUY',.02,'b'))
        a.submit_order(_req('BONK','SELL',.03,'exit'))
        snap=pa.snapshot()
        assert snap['cash']==6. and snap['pending_sol_basis']==3.
        assert snap['cost_exposure']==4.
        assert len(snap['pending_sol'])==2
        assert len(a._positions)==1 and a._positions['2']['volume_sol']==pytest.approx(.01)
        assert pa.try_debit('watcher','N',1.,1.,'other')[0]
        a.close_position_at(2,0.,'dead-pool')
        snap=pa.snapshot()
        assert len(snap['pending_sol'])==3 and snap['cash']==5.
        result=a.settle_pending_usd({'usd_per_sol':150.,'observed_at':time.time(),'source':'fixture'})
        assert all(x['ok'] for x in result)
        snap=pa.snapshot()
        assert snap['pending_sol']==[] and snap['cash']==9.5
        assert snap['realized_pnl']==.5
        assert snap['lanes']['watcher']['open_basis']==1.
        assert not a._positions

    def test_partial_shared_exit_recovery_before_native_projection(self, account, tmp_path):
        a=adapter(tmp_path)
        a.submit_order(_req('BONK','BUY',.02,'a'))
        a.submit_order(_req('BONK','BUY',.02,'b'))
        from daemon.launch_scanner import pending_sol
        reserve=pending_sol.reserve
        calls=[0]
        def fail(*args,**kwargs):
            calls[0]+=1
            if calls[0]==2: raise OSError('B not published')
            return reserve(*args,**kwargs)
        with patch.object(pending_sol,'reserve',side_effect=fail):
            with pytest.raises(OSError):
                a.submit_order(_req('BONK','SELL',.03,'exit'))
        assert len(pa.snapshot()['pending_sol'])==1
        assert len(a._positions)==1  # in-memory is not proof of committed state
        b=adapter(tmp_path)
        assert len(pa.snapshot()['pending_sol'])==2
        assert len(b._positions)==1 and b._positions['2']['volume_sol']==pytest.approx(.01)
        assert b.submit_order(_req('BONK','SELL',.03,'exit')).ticket==3
        assert len(b._fills_path().read_text().splitlines())==3

    def test_persisted_mode_blocks_noninjected_restart_and_old_process(self, account, tmp_path):
        old=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}))
        old.connect({})
        a=adapter(tmp_path)
        a.submit_order(_req('BONK','BUY',.02,'a'))
        with pytest.raises(ValueError,match='requires authoritative'):
            _adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}))
        with pytest.raises(ValueError,match='requires authoritative'):
            old.close_position_at(1,0.)
        a.close_position_at(1,0.)
        with pytest.raises(ValueError,match='requires authoritative'):
            old.submit_order(_req('BONK','BUY',1.,'unfunded'))
        assert pa.snapshot()['cash']==8.

    def test_distinct_books_do_not_alias_one_shared_debit(self, account, tmp_path):
        a=adapter(tmp_path/'a'); b=adapter(tmp_path/'b')
        a.submit_order(_req('BONK','BUY',.02,'same'))
        b.submit_order(_req('BONK','BUY',.02,'same'))
        assert pa.snapshot()['cash']==6.
        assert a._positions['1']['shared_pos_id']!=b._positions['1']['shared_pos_id']

    def test_exit_uses_entry_mint_despite_symbol_registry_change(self, account, tmp_path):
        a=adapter(tmp_path)
        a.submit_order(_req('BONK','BUY',.02,'a'))
        mint=a._positions['1']['shared_mint']
        a._market.register('BONK','different-mint')
        with pytest.raises(BrokerError,match='request conflict'):
            a.submit_order(_req('BONK','BUY',.02,'a'))
        a.close_position_at(1,0.)
        assert pa.snapshot()['pending_sol'][0]['mint']==mint
        assert not a._intent_path().exists()

    def test_account_generation_loss_refuses_bound_book(self, account, tmp_path):
        import shutil
        a=adapter(tmp_path/'native')
        a.submit_order(_req('BONK','BUY',.02,'a'))
        shutil.rmtree(pa.ACCOUNT_DIR)
        with pytest.raises(ValueError,match='account is missing'):
            adapter(tmp_path/'native')
        assert not __import__('pathlib').Path(pa.LEDGER_PATH).exists()
        pa.snapshot()  # Explicit separate genesis does not authorize rebinding.
        with pytest.raises(ValueError,match='generation mismatch'):
            adapter(tmp_path/'native')

    def test_priced_close_sell_and_new_entry_refuse_remapped_asset(self, account, tmp_path):
        a=adapter(tmp_path)
        a.submit_order(_req('BONK','BUY',.02,'a'))
        a._market.register('BONK','other-mint')
        a._market.usd['BONK']=100.
        before=a._state_path().read_bytes()
        with pytest.raises(BrokerError,match='immutable entry mint'):
            a.close_position(1)
        for side in ('BUY','SELL'):
            with pytest.raises(BrokerError,match='immutable entry mint'):
                a.submit_order(_req('BONK',side,.01,'new-'+side))
        assert a._state_path().read_bytes()==before
        assert pa.snapshot()['cash']==8.

    def test_actual_scanner_two_tier_real_adapter_shared_pool(self, account, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from daemon.launch_scanner import LaunchScanner, killswitch, veto, scoreboard
        monkeypatch.setenv('FOREX_AGENT_TESTING','1')
        monkeypatch.setattr(killswitch,'gate',lambda lane:True)
        monkeypatch.setattr(veto,'veto_check',lambda *a,**k:{'passed':True,'vetoes':[]})
        monkeypatch.setattr(scoreboard,'load_degen_closes',lambda *a:[])
        a=adapter(tmp_path)
        mint=a._market.mint_for('BONK')
        scanner=LaunchScanner.__new__(LaunchScanner)
        state={'degen_positions':{}}
        card={'entry':None,'note':'','tier':'early','source':'pumpfun',
              'extra':{'complete':True}}
        degen=SimpleNamespace(entry_sol=.04,stop_loss_pct=30.,take_profit_pct=200.,
             max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=True,
             tier_b_max_hold_hours=24.,speed_game_min_age_min=0.)
        assert scanner._paper_enter(None,a,state,card,degen,mint,'BONK')
        assert len(state['degen_positions'])==2
        assert pa.snapshot()['cash']==6.
        assert pa.snapshot()['lanes']['main']['open_count']==2
        tickets={p['ticket'] for p in state['degen_positions'].values()}
        assert tickets=={1,2}
        assert card['entry']['volume_sol']==.04
        # Real scanner time-stop decision, actual adapter closes and economics.
        for position in state['degen_positions'].values():
            position['entry_ts']=time.time()-30*3600
        degen.trail_trigger_pct=50.
        degen.trail_drawdown_pct=30.
        published=[]
        from agent import events
        with patch.object(events,'publish',side_effect=published.append), \
                patch.object(scanner,'_grade_deployer_close'), \
                patch.object(scanner,'_record_narrative_close'), \
                patch.object(scanner,'_backtest_record_paper_close'):
            scanner._manage_exits(SimpleNamespace(degen=degen),a,state)
        assert state['degen_positions']=={}
        closes=[e for e in published if e.get('event')=='position.closed']
        assert len(closes)==2
        assert {e['reason'] for e in closes}=={'degen_max_hold'}
        assert all(e['profit']==0. for e in closes)
        pending=pa.snapshot()['pending_sol']
        assert len(pending)==2 and pa.snapshot()['cash']==6.
        assert all(x['ok'] for x in a.settle_pending_usd({
             'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}))
        assert pa.snapshot()['cash']==10.
