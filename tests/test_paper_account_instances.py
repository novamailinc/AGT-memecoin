"""Exact lot instances under one lane, with legacy shape and policy preserved."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from daemon.launch_scanner import paper_account as pa, account_marks


@pytest.fixture
def account(tmp_path, monkeypatch):
    folder = tmp_path / 'paper_account'
    monkeypatch.setattr(pa, 'ACCOUNT_DIR', str(folder))
    monkeypatch.setattr(pa, 'LEDGER_PATH', str(folder / 'ledger.json'))
    monkeypatch.setattr(pa, 'JOURNAL_PATH', str(folder / 'events.jsonl'))
    monkeypatch.setattr(pa, 'LOCK_FILE', str(folder / '.lock'))
    for key in ('PAPER_ACCOUNT_LANE_CAP',
                'PAPER_ACCOUNT_PROVIDER_HEALTH_GATE_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('PAPER_ACCOUNT_START', '10')
    # Mandatory-guard migration (disclosed, 2026-11-24): the shared account
    # harness now runs with gates ON and TEST-FIXTURE limits (not owner
    # values), matching production where entries always reserve under
    # configured gates. Marks resolve through the real pipeline with
    # at-cost fixture prices; no network. Tests asserting gate-refusal
    # semantics set their own env and do not use this fixture.
    monkeypatch.setenv('PAPER_ACCOUNT_GATES_ENABLED', '1')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_DAILY_LOSS_USD', '1000000')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_DRAWDOWN_PCT', '99')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_EXPOSURE_PCT', '100')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_SIMULTANEOUS', '1000')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_POSITIONS', '1000')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_DATA_AGE_S', '100000')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD', '1000000')
    monkeypatch.setenv('PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD', '1000000')
    from tests import _guard_fixture as _gf
    from daemon.solana_ids import WSOL_MINT
    _feed = _gf.install_price_feed(at_cost=True)
    # FX/settlement flows mark pending SOL through the same seam; supply a
    # disclosed fixture SOL/USD price so the denied curve is never needed.
    _feed.set({WSOL_MINT: (150.0, time.time())})
    yield pa
    _feed.uninstall()


class TestPaperAccountInstances:
    # Generation binding (2026-11-27): credit()/write_down() now
    # require proof of the generation the entry was debited under.
    # The lanes capture it atomically at debit time via
    # pa.entry_binding(); these tests do the same. Every economic
    # assertion below is unchanged — only the settlement proof is
    # supplied, exactly as production callers do.
    def _bind(self, lane, mint, op_id, pos_id):
        b = pa.entry_binding(lane, mint, op_id, pos_id)
        assert b, 'no entry binding for %s/%s' % (op_id, pos_id)
        return b

    def test_same_lane_exact_lots_cash_and_legacy_isolation(self, account, monkeypatch):
        monkeypatch.setenv('PAPER_ACCOUNT_LANE_CAP', '5')
        ok, _, a = pa.try_debit_instance('scanner', 'M', 1., 100., 'a')
        assert ok
        ok, _, b = pa.try_debit_instance('scanner', 'M', 2., 100., 'b')
        assert ok and b != a
        snap = pa.snapshot()
        assert snap['cash'] == 7. and snap['cost_exposure'] == 3.
        assert snap['lanes']['scanner']['open_count'] == 2
        assert {p['mint'] for p in snap['lanes']['scanner']['positions']} == {'M'}
        assert not pa.try_debit('scanner', 'M', 1., 100., 'legacy')[0]
        assert not pa.try_debit_instance('scanner', 'M', 3., 100., 'cap')[0]
        monkeypatch.delenv('PAPER_ACCOUNT_LANE_CAP')
        assert pa.try_debit('watcher', 'N', 7., 100., 'watcher')[0]
        assert not pa.try_debit_instance('scanner', 'M', .01, 1., 'empty')[0]
        assert pa.try_debit_instance('scanner', 'M', 1., 100., 'a') == (True, 'ok (retry of applied op)', a)
        assert not pa.try_debit('scanner', 'M', 1., 100., 'a')[0]

    def test_exact_ticket_partial_basis_reopen_and_conflicts(self, account):
        a = pa.try_debit_instance('scanner', 'M', 1., 100., 'a')[2]
        ba = self._bind('scanner', 'M', 'a', a)
        b = pa.try_debit_instance('scanner', 'M', 2., 100., 'b')[2]
        bb = self._bind('scanner', 'M', 'b', b)
        kind = 'current_price_simulated_fill'
        assert pa.credit('scanner', 'M', 1.5, 'sell-a', pos_id=a, qty=100., price_kind=kind,
                         expected_account_id=ba, entry_op_id='a')[0]
        assert pa.credit('scanner', 'M', 1., 'sell-b', pos_id=b, qty=50., price_kind=kind,
                         expected_account_id=bb, entry_op_id='b')[0]
        snap = pa.snapshot()
        assert snap['cash'] == 9.5 and snap['realized_pnl'] == .5
        assert snap['cost_exposure'] == 1.
        c = pa.try_debit_instance('scanner', 'M', .5, 100., 'c')[2]
        bc = self._bind('scanner', 'M', 'c', c)
        assert c not in (a,b)
        assert not pa.credit('scanner', 'M', 1., 'stale-a', pos_id=a, qty=50., price_kind=kind,
                             expected_account_id=ba, entry_op_id='a')[0]
        assert pa.credit('scanner', 'M', 1.5, 'sell-a', pos_id=a, qty=100., price_kind=kind,
                         expected_account_id=ba, entry_op_id='a')[0]
        assert not pa.credit('scanner', 'M', 99., 'sell-a', pos_id=a, qty=100., price_kind=kind,
                             expected_account_id=ba, entry_op_id='a')[0]
        assert not pa.credit('scanner', 'N', 1., 'wrong-mint', pos_id=b, qty=10., price_kind=kind,
                             expected_account_id=bb, entry_op_id='b')[0]
        snap = pa.snapshot()
        assert snap['cash'] == 9. and snap['cost_exposure'] == 1.5
        # A credit with no pos_id (and no binding) is refused — but
        # the refusal writes a terminal generation-refusal marker
        # that blocks later ENTRIES in this account by design, so
        # this malformed-call assertion goes last.
        assert not pa.credit('scanner', 'M', 1., 'wrong', qty=50., price_kind=kind)[0]

    def test_marks_use_real_mint_and_distinct_instance_keys(self, account):
        a = pa.try_debit_instance('scanner', 'M', 1., 100., 'a')[2]
        b = pa.try_debit_instance('scanner', 'M', 2., 200., 'b')[2]
        bb = self._bind('scanner', 'M', 'b', b)
        calls = []
        def price(mints):
            calls.append(mints)
            return {'M': (.02, time.time())}
        marks = account_marks.collect_marks(price_fn=price)
        assert calls == [['M']]
        assert set(marks['scanner']) == {a,b}
        assert marks['scanner'][a]['qty'] == 100.
        assert marks['scanner'][b]['qty'] == 200.
        assert pa.snapshot(marks)['marked_equity'] == 13.
        assert pa.credit('scanner', 'M', 1., 'partial', pos_id=b, qty=100.,
                         price_kind='current_price_simulated_fill',
                         expected_account_id=bb, entry_op_id='b')[0]
        assert 'qty' in pa.snapshot(marks)['marks_status']
        fresh = account_marks.collect_marks(price_fn=price)
        assert pa.snapshot(fresh)['marked_equity'] == 12.

    @pytest.mark.parametrize('operation', ['debit', 'credit', 'write_down'])
    def test_wal_replay_and_bucket_rebuild(self, account, operation):
        a = pa.try_debit_instance('scanner', 'M', 1., 100., 'a')[2]
        ba = self._bind('scanner', 'M', 'a', a)
        b = pa.try_debit_instance('scanner', 'M', 2., 200., 'b')[2]
        bb = self._bind('scanner', 'M', 'b', b)
        with patch.object(pa, '_save_locked', side_effect=OSError('state-write')):
            with pytest.raises(OSError):
                if operation == 'debit':
                    pa.try_debit_instance('scanner', 'M', 1., 100., 'c')
                elif operation == 'credit':
                    pa.credit('scanner', 'M', 1.5, 'close', pos_id=a,
                              price_kind='current_price_simulated_fill',
                              expected_account_id=ba, entry_op_id='a')
                else:
                    pa.write_down('scanner', 'M', 'wd', pos_id=b, qty=100.,
                                  expected_account_id=bb, entry_op_id='b')
        snap = pa.snapshot()
        assert snap['degraded'] is None
        assert snap['cash'] == {'debit':6., 'credit':8.5, 'write_down':7.}[operation]
        assert snap['cost_exposure'] == {'debit':4., 'credit':2., 'write_down':2.}[operation]
        # Derive cash buckets from original genesis with instance events intact.
        data = json.loads(Path(pa.LEDGER_PATH).read_text())
        data.pop('cash_spendable'); data.pop('cash_estimated')
        Path(pa.LEDGER_PATH).write_text(json.dumps(data))
        rebuilt = pa.snapshot()
        assert rebuilt['degraded'] is None
        assert rebuilt['cash_spendable'] == snap['cash_spendable']
        if operation == 'credit':
            assert not pa.credit('scanner', 'M', 77., 'close', pos_id=a,
                                 price_kind='current_price_simulated_fill',
                                 expected_account_id=ba, entry_op_id='a')[0]

    def test_legacy_wal_unchanged_and_cannot_alias_instance(self, account):
        a = pa.try_debit('watcher', 'M', 1., 100., 'old')[2]
        bold = self._bind('watcher', 'M', 'old', a)
        assert not pa.try_debit_instance('watcher', 'M', 1., 100., 'old')[0]
        assert not pa.try_debit_instance('watcher', 'M', 1., 100., 'new')[0]
        data = json.loads(Path(pa.LEDGER_PATH).read_text())
        assert set(data['lanes']['watcher']['open']) == {'M'}
        entry = [json.loads(x) for x in Path(pa.JOURNAL_PATH).read_text().splitlines()
                 if json.loads(x).get('event') == 'debit'][0]
        assert 'instance' not in entry
        assert pa.credit('watcher', 'M', 1., 'old-close', pos_id=a,
                         price_kind='current_price_simulated_fill',
                         expected_account_id=bold, entry_op_id='old')[0]

    def test_real_process_death_then_fresh_recovery(self, account, tmp_path):
        root = str(Path(__file__).resolve().parents[1])
        child = '''
import os,sys
os.environ['FOREX_AGENT_HOME']=sys.argv[1]
os.environ['PAPER_ACCOUNT_START']='10'
from tests import _guard_fixture as g
g.install_price_feed(at_cost=True)
from daemon.launch_scanner import paper_account as pa
pa.try_debit_instance('scanner','M',1.,100.,'a')
bind_a=pa.entry_binding('scanner','M','a','scanner/M#1')
assert bind_a, 'no entry binding for the first debit'
pa.try_debit_instance('scanner','M',2.,200.,'b')
pa._save_locked=lambda led: os._exit(73)
pa.credit('scanner','M',1.5,'exit-a',pos_id='scanner/M#1',price_kind='current_price_simulated_fill',expected_account_id=bind_a,entry_op_id='a')
'''
        result = subprocess.run([sys.executable, '-c', child, str(tmp_path)], cwd=root)
        assert result.returncode == 73
        verify = '''
import os,sys,json
os.environ['FOREX_AGENT_HOME']=sys.argv[1]
from pathlib import Path
from tests import _guard_fixture as g
g.install_price_feed(at_cost=True)
from daemon.launch_scanner import paper_account as pa
s=pa.snapshot()
assert s['degraded'] is None
assert s['cash']==8.5 and s['cost_exposure']==2.
assert s['lanes']['scanner']['positions'][0]['pos_id']=='scanner/M#2'
# Fresh process: no thread-local debit capture. The binding is the
# ledger's own account generation (same book, same generation).
gen=json.loads(Path(pa.LEDGER_PATH).read_text())['account_id']
assert pa.credit('scanner','M',1.5,'exit-a',pos_id='scanner/M#1',price_kind='current_price_simulated_fill',expected_account_id=gen,entry_op_id='a')[0]
assert not pa.credit('scanner','M',15.,'exit-a',pos_id='scanner/M#1',price_kind='current_price_simulated_fill',expected_account_id=gen,entry_op_id='a')[0]
'''
        subprocess.run([sys.executable,'-c',verify,str(tmp_path)],cwd=root,check=True)

    def test_two_processes_cannot_double_spend_instance_funds(self, account, tmp_path):
        root = str(Path(__file__).resolve().parents[1])
        child = '''
import os,sys,json,time
from pathlib import Path
os.environ['FOREX_AGENT_HOME']=sys.argv[1]
os.environ['PAPER_ACCOUNT_START']='10'
from daemon.launch_scanner import paper_account as pa
while not Path(sys.argv[1]+'/go').exists(): time.sleep(.005)
result=pa.try_debit_instance('scanner','M',7.,100.,sys.argv[2])
Path(sys.argv[1]+'/'+sys.argv[2]).write_text(json.dumps(result))
'''
        children = [subprocess.Popen([sys.executable,'-c',child,str(tmp_path),key],cwd=root)
                    for key in ('a','b')]
        (tmp_path / 'go').touch()
        for proc in children:
            assert proc.wait(timeout=20)==0
        results = [json.loads((tmp_path / key).read_text()) for key in ('a','b')]
        assert sum(x[0] for x in results)==1
        snap = pa.snapshot()
        assert snap['cash']==3. and snap['cost_exposure']==7.
        assert snap['lanes']['scanner']['open_count']==1

    @pytest.mark.parametrize('replay', [False, True])
    def test_instance_write_down_exact_operation_shape(self, account, replay):
        a = pa.try_debit_instance('scanner','M',1.,100.,'a')[2]
        ba = self._bind('scanner', 'M', 'a', a)
        b = pa.try_debit_instance('scanner','M',1.,100.,'b')[2]
        bb = self._bind('scanner', 'M', 'b', b)
        if replay:
            with patch.object(pa,'_save_locked',side_effect=OSError('stop')):
                with pytest.raises(OSError):
                    pa.write_down('scanner','M','wd',pos_id=a,qty=25.,reason='gone',
                                  expected_account_id=ba, entry_op_id='a')
            assert pa.snapshot()['degraded'] is None
        else:
            assert pa.write_down('scanner','M','wd',pos_id=a,qty=25.,reason='gone',
                                 expected_account_id=ba, entry_op_id='a')[0]
        assert pa.write_down('scanner','M','wd',pos_id=a,qty=25.,reason='gone',
                             expected_account_id=ba, entry_op_id='a')[0]
        assert not pa.write_down('scanner','M','wd',pos_id=a,qty=75.,reason='gone',
                                 expected_account_id=ba, entry_op_id='a')[0]
        assert not pa.write_down('scanner','M','wd',pos_id=b,qty=25.,reason='gone',
                                 expected_account_id=bb, entry_op_id='b')[0]
        assert not pa.write_down('scanner','M','wd',pos_id=a,qty=25.,reason='other',
                                 expected_account_id=ba, entry_op_id='a')[0]
        assert not pa.write_down('scanner','M','a',pos_id=a,qty=25.,reason='gone',
                                 expected_account_id=ba, entry_op_id='a')[0]
        snap = pa.snapshot()
        assert snap['cost_exposure']==1.75 and snap['unresolved_cost']==.25

    @pytest.mark.parametrize('instance', [False, True])
    def test_terminal_unresolved_new_operation_does_not_crash(self, account, instance):
        debit = pa.try_debit_instance if instance else pa.try_debit
        pid = debit('scanner','M',1.,100.,'a')[2]
        bind = self._bind('scanner', 'M', 'a', pid)
        assert pa.write_down('scanner','M','wd1',pos_id=pid,reason='gone',
                             expected_account_id=bind, entry_op_id='a')[0]
        assert pa.write_down('scanner','M','wd2',pos_id=pid,reason='gone',
                             expected_account_id=bind, entry_op_id='a')[0]
        assert pa.write_down('scanner','M','wd2',pos_id=pid,reason='gone',
                             expected_account_id=bind, entry_op_id='a')[0]
        if instance:
            assert not pa.write_down('scanner','M','wd2',pos_id=pid,qty=50.,reason='gone',
                                     expected_account_id=bind, entry_op_id='a')[0]
        snap = pa.snapshot()
        assert snap['degraded'] is None and snap['unresolved_cost']==1.
        assert snap['cash']==9. and snap['cost_exposure']==0.
