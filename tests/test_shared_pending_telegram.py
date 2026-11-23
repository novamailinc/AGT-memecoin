"""Read-only local Telegram presentation of actual shared native pending proceeds."""
import time
from types import SimpleNamespace
from daemon.launch_scanner import paper_account as pa, pending_sol
from daemon.telegram import commands
from tests.test_paper_account_instances import account


class TestSharedPendingTelegram:
    def test_actual_snapshot_positions_and_account_never_hide_pending(self, account, monkeypatch):
        pid=pa.try_debit_instance('main','M',2.,100.,'entry')[2]
        assert pending_sol.reserve(pa,'main','M',pid,100.,.02,1,.02,time.time()-1,'sold')[0]
        monkeypatch.setattr(commands,'control_only',lambda:False)
        snapshot=commands._demo_snapshot()
        assert snapshot['cash']==8. and len(snapshot['pending_sol'])==1
        count,lines=commands._demo_positions_lines(snapshot)
        assert count==0 and 'unspendable' in '\n'.join(lines)
        messages=[]
        ctx=SimpleNamespace(send=lambda chat,text,*a:messages.append(text))
        commands._cmd_account(ctx,1,1,1,['demo'])
        body='\n'.join(messages)
        assert 'Spendable cash: $8.00' in body
        assert 'Pending SOL settlements: 1' in body
        assert 'retained basis $2.00' in body
