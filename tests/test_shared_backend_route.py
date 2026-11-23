"""Existing shared-account opt-in reaches actual backend constructor offline."""
import time
from types import SimpleNamespace

import pytest
from agent.tools import backend
from broker.memecoin.adapter import MemecoinAdapter
from tests.test_broker_memecoin import FakeMarket, _req
from tests.test_paper_account_instances import account
from daemon.launch_scanner import paper_account as pa


class TestSharedBackendRoute:
    def test_existing_flag_routes_real_factory_and_downgrade_refuses(self, account, tmp_path, monkeypatch):
        cfg=SimpleNamespace(mode='dry_run',broker=SimpleNamespace(provider='memecoin',
             memecoin=SimpleNamespace(data_dir=str(tmp_path/'native'),symbols={'BONK':'MINT'})))
        monkeypatch.setattr(backend,'app_config',lambda:cfg)
        monkeypatch.setenv('PAPER_ACCOUNT_ENABLED','1')
        a=backend._build_adapter()
        assert isinstance(a,MemecoinAdapter) and a._shared_account is pa
        market=FakeMarket({'SOL':100.,'BONK':.0001})
        market.register('BONK','MINT')
        a._market=market
        a._fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}
        a.submit_order(_req('BONK','BUY',.02,'entry'))
        assert pa.snapshot()['cash']==8.
        monkeypatch.delenv('PAPER_ACCOUNT_ENABLED')
        with pytest.raises(ValueError,match='requires authoritative'):
            backend._build_adapter()

    def test_unbound_default_stays_standalone(self, account, tmp_path, monkeypatch):
        monkeypatch.delenv('PAPER_ACCOUNT_ENABLED',raising=False)
        cfg=SimpleNamespace(mode='dry_run',broker=SimpleNamespace(provider='memecoin',
             memecoin=SimpleNamespace(data_dir=str(tmp_path/'native'))))
        monkeypatch.setattr(backend,'app_config',lambda:cfg)
        assert backend._build_adapter()._shared_account is None

    def test_actual_factory_scanner_exit_report_and_settlement_followthrough(self, account, tmp_path, monkeypatch):
        from daemon.launch_scanner import LaunchScanner, killswitch, veto, scoreboard
        from daemon.telegram import commands
        from agent import events
        from unittest.mock import patch
        cfg=SimpleNamespace(mode='dry_run',broker=SimpleNamespace(provider='memecoin',
             memecoin=SimpleNamespace(data_dir=str(tmp_path/'native'),symbols={'BONK':'MINT'})))
        monkeypatch.setattr(backend,'app_config',lambda:cfg)
        monkeypatch.setenv('PAPER_ACCOUNT_ENABLED','1');monkeypatch.setenv('FOREX_AGENT_TESTING','1')
        a=LaunchScanner._fresh_adapter()
        market=FakeMarket({'SOL':100.,'BONK':.0001});market.register('BONK','MINT')
        a._market=market
        a._fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}
        monkeypatch.setattr(killswitch,'gate',lambda lane:True)
        monkeypatch.setattr(veto,'veto_check',lambda *a,**k:{'passed':True,'vetoes':[]})
        monkeypatch.setattr(scoreboard,'load_degen_closes',lambda *a:[])
        scanner=LaunchScanner.__new__(LaunchScanner)
        state={'degen_positions':{}}
        card={'entry':None,'note':'','tier':'early','source':'pumpfun','extra':{'complete':True}}
        degen=SimpleNamespace(entry_sol=.04,stop_loss_pct=30.,take_profit_pct=200.,
             max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=True,
             tier_b_max_hold_hours=24.,speed_game_min_age_min=0.,
             trail_trigger_pct=50.,trail_drawdown_pct=30.)
        assert scanner._paper_enter(None,a,state,card,degen,'MINT','BONK')
        for pos in state['degen_positions'].values(): pos['entry_ts']=time.time()-30*3600
        with patch.object(events,'publish'),patch.object(scanner,'_grade_deployer_close'), \
             patch.object(scanner,'_record_narrative_close'),patch.object(scanner,'_backtest_record_paper_close'):
            scanner._manage_exits(SimpleNamespace(degen=degen),a,state)
        monkeypatch.setattr(commands,'control_only',lambda:False)
        snapshot=commands._demo_snapshot()
        assert snapshot['cash']==6. and len(snapshot['pending_sol'])==2
        assert commands._demo_positions_lines(snapshot)[0]==0
        # Later process uses same real factory/book binding, no native rollback.
        b=LaunchScanner._fresh_adapter()
        b._fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}
        assert all(item['ok'] for item in b.retry_pending_usd())
        assert commands._demo_snapshot()['cash']==10.
        assert commands._demo_snapshot()['pending_sol']==[]
        assert not b._positions
