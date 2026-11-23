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
