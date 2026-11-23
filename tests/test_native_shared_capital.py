"""Offline bridge primitives only, not native/scanner admission integration."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from broker.memecoin.shared_capital import (
    entry_plan, exit_plan, apply_entry, apply_exit, validate_fx, CapitalConflict)
from daemon.launch_scanner import paper_account as pa
from tests.test_paper_account_instances import account


class TestNativeSharedCapital:
    def test_entry_exact_receipt_recovery_fx_not_refetched(self, account):
        evidence = {'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture:observed'}
        plan = entry_plan('scanner','M',1,100.,.02,evidence,'entry')
        with patch.object(pa,'_save_locked',side_effect=OSError('after WAL')):
            with pytest.raises(OSError):
                apply_entry(plan,pa)
        # Original plan stays pinned even after market/clock changed.
        with patch('time.time',return_value=time.time()+300.):
            receipt, error = apply_entry(plan,pa)
        assert error is None and receipt['stake']==2.
        assert receipt['pos_id']=='scanner/M#1'
        assert pa.snapshot()['cash']==8.
        assert apply_entry(plan,pa)[0]==receipt
        changed = dict(plan,stake=3.)
        with pytest.raises(CapitalConflict):
            apply_entry(changed,pa)
        assert pa.snapshot()['cash']==8.

    def test_moved_fx_pnl_uses_entry_basis_not_converted_sol_profit(self, account):
        now=time.time()
        entry=entry_plan('scanner','M',1,100.,.02,
                         {'usd_per_sol':100.,'observed_at':now,'source':'fixture'},'entry')
        receipt,_=apply_entry(entry,pa)
        allocation={'ticket':1,'tokens':100.,'basis_sol':.02,'proceeds_sol':.02}
        settle=exit_plan('scanner','M',allocation,receipt['pos_id'],
                         {'usd_per_sol':150.,'observed_at':now,'source':'fixture'},'exit')
        receipt,_=apply_exit(settle,pa)
        assert receipt['realized']==1.
        assert pa.snapshot()['cash']==11.
        assert apply_exit(settle,pa)[0]==receipt

    @pytest.mark.parametrize('bad', [None,{}, {'usd_per_sol':0,'observed_at':1,'source':'x'},
                                    {'usd_per_sol':float('nan'),'observed_at':1,'source':'x'},
                                    {'usd_per_sol':1,'observed_at':-100,'source':'x'}])
    def test_fx_missing_or_invalid_never_funds_entry(self, account, bad):
        with pytest.raises(CapitalConflict):
            entry_plan('scanner','M',1,100.,.02,bad,'entry')
        assert pa.snapshot()['cash']==10.

    def test_authoritative_receipt_requires_wal_agreement(self, account):
        now=time.time()
        plan=entry_plan('scanner','M',1,100.,.02,
                        {'usd_per_sol':100.,'observed_at':now,'source':'fixture'},'entry')
        receipt,_=apply_entry(plan,pa)
        path=Path(pa.LEDGER_PATH)
        data=json.loads(path.read_text())
        data['ops']['entry']['stake']=9.
        path.write_text(json.dumps(data))
        with pytest.raises(CapitalConflict,match='disagreement'):
            apply_entry(plan,pa)
        assert json.loads(path.read_text())['cash']==8.

    @pytest.mark.parametrize('target',['events.jsonl','ledger.json','directory'])
    def test_receipt_has_strict_durability_barrier(self, account, target):
        import os, stat
        plan=entry_plan('scanner','M',1,100.,.02,
                        {'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'},'entry')
        receipt,_=apply_entry(plan,pa)
        real=os.fsync
        attempted=[]
        def fail(fd):
            name=Path(os.readlink('/proc/self/fd/%d'%fd)).name
            isdir=stat.S_ISDIR(os.fstat(fd).st_mode)
            attempted.append((name,isdir))
            if (target=='directory' and isdir) or name==target:
                raise OSError('durability failure')
            return real(fd)
        with patch.object(os,'fsync',side_effect=fail):
            with pytest.raises(OSError):
                apply_entry(plan,pa)
        assert attempted
        assert apply_entry(plan,pa)[0]==receipt

    def test_selective_new_wal_fsync_failure_never_returns_receipt(self, account):
        import os
        plan=entry_plan('scanner','M',1,100.,.02,
                        {'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'},'entry')
        pa.snapshot()
        real=os.fsync
        def fail(fd):
            if Path(os.readlink('/proc/self/fd/%d'%fd)).name=='events.jsonl':
                raise OSError('WAL fsync failed')
            return real(fd)
        with patch.object(os,'fsync',side_effect=fail):
            with pytest.raises(OSError):
                apply_entry(plan,pa)
        receipt,_=apply_entry(plan,pa)
        assert receipt['stake']==2. and pa.snapshot()['cash']==8.
