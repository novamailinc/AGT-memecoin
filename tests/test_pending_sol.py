"""Unspendable native sale proceeds are SOL holdings, never token marks."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from daemon.launch_scanner import paper_account as pa, pending_sol as pending, account_marks
from daemon.solana_ids import WSOL_MINT
from tests.test_paper_account_instances import account


def reserve(pid, qty=100., proceeds=.02, op='pending'):
    return pending.reserve(pa,'scanner','M',pid,qty,proceeds,1,.02,time.time()-1,op)


class TestPendingSOL:
    def test_sold_units_removed_no_token_mark_no_cash_until_fx(self, account, monkeypatch):
        monkeypatch.setenv('PAPER_ACCOUNT_LANE_CAP','2')
        pid=pa.try_debit_instance('scanner','M',2.,100.,'entry')[2]
        assert reserve(pid)[0]
        snap=pa.snapshot()
        assert snap['cash']==8. and snap['cash_spendable']==8.
        assert snap['cost_exposure']==2. and snap['pending_sol_basis']==2.
        assert snap['lanes']['scanner']['positions']==[]
        assert snap['pending_sol'][0]['proceeds_sol']==.02
        assert snap['marked_equity'] is None
        assert not pa.try_debit_instance('scanner','N',.01,1.,'cap')[0]
        seen=[]
        def prices(mints):
            seen.append(mints)
            return {WSOL_MINT:(150.,time.time()),'M':(999.,time.time())}
        marks=account_marks.collect_marks(price_fn=prices)
        assert seen==[[WSOL_MINT]]
        assert pa.snapshot(marks)['marked_equity']==11.
        fx={'usd_per_sol':150.,'observed_at':time.time(),'source':'fixture'}
        assert pending.settle(pa,'scanner','M',pid,'pending',fx,3.,'settle')[0]
        snap=pa.snapshot()
        assert snap['cash']==11. and snap['cash_spendable']==11.
        assert snap['pending_sol']==[] and snap['cost_exposure']==0.
        assert snap['realized_pnl']==1.
        assert pending.settle(pa,'scanner','M',pid,'pending',fx,3.,'settle')[0]
        assert not pending.settle(pa,'scanner','M',pid,'pending',fx,30.,'settle')[0]

    @pytest.mark.parametrize('kind',['pending','settle'])
    def test_wal_replay_bucket_rebuild_and_later_trade(self, account, kind):
        pid=pa.try_debit_instance('scanner','M',2.,100.,'entry')[2]
        if kind=='settle':
            assert reserve(pid)[0]
        fx={'usd_per_sol':150.,'observed_at':time.time(),'source':'fixture'}
        with patch.object(pa,'_save_locked',side_effect=OSError('stop after WAL')):
            with pytest.raises(OSError):
                if kind=='pending': reserve(pid)
                else: pending.settle(pa,'scanner','M',pid,'pending',fx,3.,'settle')
        snap=pa.snapshot()
        assert snap['degraded'] is None
        assert snap['cash']==(8. if kind=='pending' else 11.)
        # A later unrelated entry must not be lost by recovery of pending proceeds.
        assert pa.try_debit('watcher','N',1.,10.,'other')[0]
        data=json.loads(Path(pa.LEDGER_PATH).read_text())
        data.pop('cash_spendable'); data.pop('cash_estimated')
        Path(pa.LEDGER_PATH).write_text(json.dumps(data))
        snap=pa.snapshot()
        assert snap['degraded'] is None
        assert snap['cash']==(7. if kind=='pending' else 10.)
        assert snap['lanes']['watcher']['open_basis']==1.

    def test_partial_lot_conservation_and_conflicts(self, account):
        pid=pa.try_debit_instance('scanner','M',2.,100.,'entry')[2]
        assert reserve(pid,qty=25.,proceeds=.005)[0]
        snap=pa.snapshot()
        assert snap['pending_sol_basis']==.5
        assert snap['lanes']['scanner']['positions'][0]['basis']==1.5
        assert snap['cost_exposure']==2.
        assert not reserve(pid,qty=50.,proceeds=.005)[0]
        fx={'usd_per_sol':100.,'observed_at':0.,'source':'fixture'}
        assert not pending.settle(pa,'scanner','M',pid,'pending',fx,.5,'early')[0]
        assert snap['cash']==8.

    @pytest.mark.parametrize('observation',['future','stale','bad-source'])
    def test_new_settlement_requires_actual_current_observation(self, account, observation):
        pid=pa.try_debit_instance('scanner','M',2.,100.,'entry')[2]
        assert reserve(pid)[0]
        before=Path(pa.LEDGER_PATH).read_bytes()
        journal=Path(pa.JOURNAL_PATH).read_bytes()
        fx={'usd_per_sol':999.,'observed_at':time.time(),'source':'fixture'}
        if observation=='future': fx['observed_at']+=86400
        if observation=='stale': fx['observed_at']-=600
        if observation=='bad-source': fx['source']=123
        assert not pending.settle(pa,'scanner','M',pid,'pending',fx,19.98,'settle')[0]
        assert Path(pa.LEDGER_PATH).read_bytes()==before
        assert Path(pa.JOURNAL_PATH).read_bytes()==journal

    def test_admitted_settlement_replay_does_not_reage_fx(self, account):
        pid=pa.try_debit_instance('scanner','M',2.,100.,'entry')[2]
        assert reserve(pid)[0]
        fx={'usd_per_sol':150.,'observed_at':time.time(),'source':'fixture'}
        with patch.object(pa,'_save_locked',side_effect=OSError('after WAL')):
            with pytest.raises(OSError):
                pending.settle(pa,'scanner','M',pid,'pending',fx,3.,'settle')
        with patch('time.time',return_value=time.time()+86400):
            assert pa.snapshot()['cash']==11.
            assert pending.settle(pa,'scanner','M',pid,'pending',fx,3.,'settle')[0]
