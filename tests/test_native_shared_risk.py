"""Actual RiskManager sees SOL sizing units, never USD or native genesis."""
import time
from datetime import datetime, timezone

import pytest
from broker import BrokerError
from config import RiskConfig
from core.risk import RiskManager
from core.signals import Signal
from tests.test_broker_memecoin import _adapter, FakeMarket, _req
from tests.test_paper_account_instances import account
from daemon.launch_scanner import paper_account as pa


class TestNativeSharedRisk:
    def test_real_risk_caller_matches_economically_equal_native_sol_book(self, account, tmp_path):
        fx=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}
        shared=_adapter(str(tmp_path/'shared'),FakeMarket({'SOL':100.,'BONK':.0001}),
                        shared_account=pa,fx_observer=fx,shared_mark_observer=lambda:{})
        native=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),
                        paper_start_sol=.1)
        shared.connect({});native.connect({})
        assert shared.account_info().currency=='SOL'
        assert shared.account_info().equity==.1
        config=RiskConfig(use_percent_risk_sizing=True,max_risk_per_trade=.01,
                          max_daily_loss=.03,max_open_positions=5,
                          max_total_exposure_lots=1.,max_correlated_positions=5)
        signal=Signal(symbol='BONK',timeframe='M1',direction='BUY',entry_price=.000001,
                      stop_loss=.000000999,take_profit=.000002,ema_fast=0.,ema_slow=0.,
                      rsi_value=0.,atr_value=0.,candle_time=datetime.now(timezone.utc),trigger='fixture')
        # Same real risk code/config/tick contract sees equal .1 SOL equity.
        from storage.store import Store
        left=RiskManager(config,store=Store(str(tmp_path/'first-risk.db'))).check(signal,shared)
        right=RiskManager(config,store=object()).check(signal,native)
        assert left.allowed==right.allowed and left.lot_size==right.lot_size
        assert left.lot_size==.001
        # Execute caller-sized order through actual shared admission.
        shared.submit_order(_req('BONK','BUY',left.lot_size,'risk-sized'))
        assert pa.snapshot()['cash']==9.9
        with pytest.raises(BrokerError,match='marks unavailable'):
            shared.account_info()
        pid=shared._positions['1']['shared_pos_id']
        shared._shared_mark_observer=lambda:{'main':{pid:{'value':.1,'qty':1000.,
             'pos_id':pid,'ts':time.time(),'source':'fixture:token'}}}
        assert shared.account_info().equity==.1
        assert shared.account_info().balance==pytest.approx(.099)

    def test_fx_move_changes_sol_view_not_usd_capital_or_native_genesis(self, account, tmp_path):
        rate=[100.]
        a=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}),
                    shared_account=pa,fx_observer=lambda:{'usd_per_sol':rate[0],
                     'observed_at':time.time(),'source':'fixture'},shared_mark_observer=lambda:{})
        a.connect({})
        assert a.account_info().equity==.1
        rate[0]=200.
        assert a.account_info().equity==.05
        assert a.shared_account_snapshot()['cash']==10.

    def test_existing_unbound_baseline_not_silently_reset(self, account, tmp_path):
        class Store:
            def __init__(self): self.state={'day':'existing','start_of_day_equity':10.,'recent_outcomes':[False]}
            def get_risk_state(self): return dict(self.state)
            def set_risk_state(self,patch): self.state.update(patch)
        store=Store(); before=dict(store.state)
        a=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
                    fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'},
                    shared_mark_observer=lambda:{})
        a.connect({})
        signal=Signal(symbol='BONK',timeframe='M1',direction='BUY',entry_price=.000001,
              stop_loss=.000000999,take_profit=.000002,ema_fast=0.,ema_slow=0.,rsi_value=0.,
              atr_value=0.,candle_time=datetime.now(timezone.utc),trigger='fixture')
        result=RiskManager(RiskConfig(),store=store).check(signal,a)
        assert not result.allowed and 'identity unbound' in result.reason
        assert store.state==before

    def test_persistent_store_identity_restart_crossbook_refusal(self, account, tmp_path):
        from storage.store import Store
        path=str(tmp_path/'risk.db')
        fx=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}
        def make(folder):
            a=_adapter(str(folder),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
                       fx_observer=fx,shared_mark_observer=lambda:{})
            a.connect({});return a
        signal=Signal(symbol='BONK',timeframe='M1',direction='BUY',entry_price=.000001,
              stop_loss=.000000999,take_profit=.000002,ema_fast=0.,ema_slow=0.,rsi_value=0.,
              atr_value=0.,candle_time=datetime.now(timezone.utc),trigger='fixture')
        cfg=RiskConfig(use_percent_risk_sizing=True,max_risk_per_trade=.01,
                       max_total_exposure_lots=1.)
        a=make(tmp_path/'native')
        first=RiskManager(cfg,store=Store(path)).check(signal,a)
        assert first.allowed
        restart=RiskManager(cfg,store=Store(path)).check(signal,make(tmp_path/'native'))
        assert restart.allowed and restart.lot_size==first.lot_size
        before=Store(path).get_risk_state()
        other=RiskManager(cfg,store=Store(path)).check(signal,make(tmp_path/'other'))
        assert not other.allowed and 'identity conflict' in other.reason
        assert Store(path).get_risk_state()==before

    def test_intervening_fx_then_final_cash_refusal_no_stale_cache_units(self, account, tmp_path):
        rate=[100.]
        a=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
              fx_observer=lambda:{'usd_per_sol':rate[0],'observed_at':time.time(),'source':'fixture'},
              shared_mark_observer=lambda:{})
        a.connect({})
        assert a.account_info().equity==.1
        a._sol_price_usd()  # Cache old100 FX in ordinary native pricing path.
        rate[0]=200.
        a.submit_order(_req('BONK','BUY',.02,'entry'))
        pos=a._positions['1']
        assert pos['shared_entry_usd']==4.
        assert pos['tokens']==pytest.approx(40000.)
        assert pos['entry_price_sol']==pytest.approx(.0000005)
        assert pa.try_debit('watcher','N',6.,1.,'other')[0]
        before=a._state_path().read_bytes()
        with pytest.raises(BrokerError,match='shared capital refused'):
            a.submit_order(_req('BONK','BUY',.01,'after-epoch'))
        assert a._state_path().read_bytes()==before
        assert pa.snapshot()['cash']==0.

    @pytest.mark.parametrize('failure',['read','write','visible-write'])
    def test_real_sqlite_history_errors_never_admit(self, account, tmp_path, monkeypatch, failure):
        from storage.store import Store
        store=Store(str(tmp_path/'risk.db'))
        a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
             fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'},
             shared_mark_observer=lambda:{})
        a.connect({})
        signal=Signal(symbol='BONK',timeframe='M1',direction='BUY',entry_price=.000001,
              stop_loss=.000000999,take_profit=.000002,ema_fast=0.,ema_slow=0.,rsi_value=0.,
              atr_value=0.,candle_time=datetime.now(timezone.utc),trigger='fixture')
        original=store.set_risk_state
        def write(patch):
            if failure=='visible-write': original(patch)
            raise OSError('SQLite write uncertain')
        if failure=='read':
            monkeypatch.setattr(store,'get_risk_state',lambda: (_ for _ in ()).throw(OSError('read')))
        else:
            monkeypatch.setattr(store,'set_risk_state',write)
        with pytest.raises(BrokerError,match='Shared risk history'):
            RiskManager(RiskConfig(),store=store).check(signal,a)
        assert pa.snapshot()['cash']==10.

    def test_old_sol_cache_cannot_manufacture_exit_proceeds(self, account, tmp_path):
        rate=[100.]
        a=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
             fx_observer=lambda:{'usd_per_sol':rate[0],'observed_at':time.time(),'source':'fixture'},
             shared_mark_observer=lambda:{})
        a.connect({});a._sol_price_usd()
        rate[0]=200.
        a.submit_order(_req('BONK','BUY',.02,'a'))
        assert pa.snapshot()['cash']==6.
        a.close_position(1)
        assert pa.snapshot()['pending_sol'][0]['proceeds_sol']==pytest.approx(.02)
        a.settle_pending_usd({'usd_per_sol':200.,'observed_at':time.time(),'source':'fixture'})
        assert pa.snapshot()['cash']==10.

    def test_native_pair_exit_survives_missing_usd_fx_without_fabricated_release(self, account, tmp_path):
        from broker.memecoin.adapter import SOL_MINT
        a=_adapter(str(tmp_path),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,
             fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
        a.connect({});a.submit_order(_req('BONK','BUY',.02,'a'))
        mint=a._market.mint_for('BONK')
        a._market.get_quote=lambda symbol:{'mint':mint,'price_sol':.000001,
             'price_sol_quote_mint':SOL_MINT,'ts':time.time(),'source':'fixture:native-pair'}
        a._fx_observer=lambda: (_ for _ in ()).throw(BrokerError('BROKER_UNAVAILABLE','FX missing'))
        assert a.close_position(1)==.000001
        snapshot=pa.snapshot()
        assert not a._positions and snapshot['cash']==8.
        assert snapshot['pending_sol'][0]['proceeds_sol']==pytest.approx(.02)
        assert snapshot['pending_sol_basis']==2.
        with pytest.raises(BrokerError):
            a.account_info()

    @pytest.mark.parametrize('field,value',[('mint','wrong'),('source',''),('ts',0.)])
    def test_usd_ratio_entry_rejects_unbound_token_observation(self, account, tmp_path, field, value):
        market=FakeMarket({'SOL':100.,'BONK':.0001})
        a=_adapter(str(tmp_path),market,shared_account=pa,
             fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
        a.connect({})
        quote=market.get_quote('BONK');quote[field]=value
        market.get_quote=lambda symbol:quote
        with pytest.raises(BrokerError,match='token USD observation'):
            a.submit_order(_req('BONK','BUY',.02,'a'))
        assert pa.snapshot()['cash']==10. and not a._positions
