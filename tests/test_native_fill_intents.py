"""Offline native durability prerequisite, no shared capital or external execution."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from broker.memecoin import adapter as module
from tests.test_broker_memecoin import _adapter, _req, FakeMarket


def ready(tmp_path):
    a = _adapter(str(tmp_path), FakeMarket({"SOL": 100., "BONK": .0001}))
    a.connect({})
    return a


def fills(a):
    return [json.loads(x) for x in a._fills_path().read_text().splitlines()]


class TestNativeFillIntents:
    @pytest.mark.parametrize('action', ['buy', 'sell', 'close', 'close_at'])
    @pytest.mark.parametrize('boundary', ['intent_before', 'intent_after', 'state_before',
                                         'state_after', 'fill_before', 'fill_after'])
    def test_exact_recovery_each_boundary(self, tmp_path, action, boundary):
        a = ready(tmp_path)
        if action != 'buy':
            a.submit_order(_req('BONK', 'BUY', 2., 'entry'))
        before = a._native_snapshot()
        original_write = module._write_json_atomic
        original_fill = a._journal_fill

        def write(path, data):
            name = 'intent' if path == a._intent_path() else 'state'
            if boundary == name + '_before':
                raise OSError('injected before durable write')
            original_write(path, data)
            if boundary == name + '_after':
                raise OSError('injected after visible write')

        def journal(fill):
            if boundary == 'fill_before':
                raise OSError('injected before journal')
            original_fill(fill)
            if boundary == 'fill_after':
                raise OSError('injected after journal')

        with patch.object(module, '_write_json_atomic', side_effect=write), \
                patch.object(a, '_journal_fill', side_effect=journal):
            with pytest.raises(OSError):
                if action == 'buy':
                    a.submit_order(_req('BONK', 'BUY', 1., 'op'))
                elif action == 'sell':
                    a.submit_order(_req('BONK', 'SELL', .5, 'op'))
                elif action == 'close':
                    a.close_position(1)
                else:
                    a.close_position_at(1, 0., 'dead-pool')
        pending = json.loads(a._intent_path().read_text()) if a._intent_path().exists() else None
        # Fresh Python interpreter, with no network/price access during recovery.
        code = '''
    import sys
    from broker.memecoin.adapter import MemecoinAdapter
    from broker.memecoin.transport import MemecoinMarketData
    class NoNetwork(MemecoinMarketData):
        def get_quote(self, *args):
            raise AssertionError('recovery must not fetch a price')
    a = MemecoinAdapter(data_dir=sys.argv[1], market_data=NoNetwork())
    '''
        subprocess.run([sys.executable, '-c', textwrap.dedent(code), str(a._data_dir)], check=True,
                       cwd=str(Path(__file__).resolve().parents[1]))
        b = ready(tmp_path)
        if boundary == 'intent_before':
            assert b._native_snapshot() == before
            return
        assert b._native_snapshot() == pending['after']
        assert not b._intent_path().exists()
        assert fills(b)[-1] == pending['fill']
        assert len({x['native_op_id'] for x in fills(b)}) == len(fills(b))
        again = ready(tmp_path)
        assert again._native_snapshot() == pending['after']
        if action in ('buy', 'sell'):
            result = again.submit_order(_req('BONK', action.upper(), 1. if action == 'buy' else .5, 'op'))
            assert result.ticket == pending['fill']['ticket']
            if action == 'sell':
                assert result.raw['realized_pnl_sol'] == pending['fill']['realized_pnl_sol']
                assert result.raw['closed_tickets'] == pending['fill']['closed_tickets']
            assert len(fills(again)) == (1 if action == 'buy' else 2)


    def test_fifo_exact_allocations_and_basis(self, tmp_path):
        a = ready(tmp_path)
        a.submit_order(_req('BONK', 'BUY', 1., 'a'))
        a._market.usd['BONK'] = .0002
        a.submit_order(_req('BONK', 'BUY', 2., 'b'))
        a.submit_order(_req('BONK', 'SELL', 3., 'sell'))
        lots = fills(a)[-1]['allocations']
        assert [x['ticket'] for x in lots] == [1, 2]
        assert [x['tokens'] for x in lots] == pytest.approx([1000000., 500000.])
        assert [x['basis_sol'] for x in lots] == pytest.approx([1., 1.])
        assert [x['proceeds_sol'] for x in lots] == pytest.approx([2., 1.])
        assert a._positions['2']['volume_sol'] == pytest.approx(1.)


    def test_full_lifetime_key_not_recent500(self, tmp_path):
        a = ready(tmp_path)
        result = a.submit_order(_req('BONK', 'BUY', .001, 'ancient'))
        for n in range(501):
            a.submit_order(_req('BONK', 'BUY', .001, str(n)))
        a._idempotency_path().unlink()
        b = ready(tmp_path)
        assert b.submit_order(_req('BONK', 'BUY', .001, 'ancient')).ticket == result.ticket
        assert len(fills(b)) == 502


    @pytest.mark.parametrize('target', ['state', 'intent', 'journal'])
    def test_corruption_not_fresh_capital(self, tmp_path, target):
        a = ready(tmp_path)
        if target == 'intent':
            with patch.object(a, '_save_state', side_effect=OSError('stop')):
                with pytest.raises(OSError):
                    a.submit_order(_req('BONK', 'BUY', 1., 'a'))
            a._intent_path().write_text('{broken')
        else:
            a.submit_order(_req('BONK', 'BUY', 1., 'a'))
            (a._state_path() if target == 'state' else a._fills_path()).write_text('{broken')
        if target == 'journal':
            b = ready(tmp_path)
            with pytest.raises(ValueError):
                b.submit_order(_req('BONK', 'BUY', 1., 'b'))
            assert not b._intent_path().exists()
        else:
            with pytest.raises(ValueError):
                ready(tmp_path)


    def test_pending_conflict_freezes_without_overwrite(self, tmp_path):
        a = ready(tmp_path)
        a.submit_order(_req('BONK', 'BUY', 1., 'a'))
        with patch.object(a, '_journal_fill', side_effect=OSError('stop')):
            with pytest.raises(OSError):
                a.submit_order(_req('BONK', 'BUY', 1., 'b'))
        state = json.loads(a._state_path().read_text())
        state['balance_sol'] = 123.
        a._state_path().write_text(json.dumps(state))
        with pytest.raises(ValueError, match='conflicts with state'):
            ready(tmp_path)
        assert json.loads(a._state_path().read_text())['balance_sol'] == 123.


    @pytest.mark.parametrize('target', ['native_pending.json', 'paper_state.json', 'fills.jsonl'])
    @pytest.mark.parametrize('failure', ['file_fsync', 'replace', 'dir_fsync'])
    def test_os_failure_never_means_rollback(self, tmp_path, target, failure):
        a = ready(tmp_path)
        real_fsync, real_replace, real_dir = os.fsync, os.replace, module._fsync_dir
        last = [None]

        def fsync(fd):
            path = os.readlink('/proc/self/fd/%d' % fd)
            if failure == 'file_fsync' and Path(path).name.startswith(target + '.tmp.'):
                raise OSError('file fsync failed')
            return real_fsync(fd)

        def replace(src, dst):
            if failure == 'replace' and Path(dst).name == target:
                raise OSError('replace failed')
            real_replace(src, dst)
            last[0] = Path(dst).name

        def syncdir(path):
            if failure == 'dir_fsync' and last[0] == target:
                raise OSError('directory fsync failed after replace')
            real_dir(path)

        with patch.object(os, 'fsync', side_effect=fsync), \
                patch.object(os, 'replace', side_effect=replace), \
                patch.object(module, '_fsync_dir', side_effect=syncdir):
            with pytest.raises(OSError):
                a.submit_order(_req('BONK', 'BUY', 1., 'op'))
        pending = a._intent_path().exists()
        b = ready(tmp_path)
        if pending:
            assert b._balance_sol == 9.
            assert len(fills(b)) == 1
            assert b.submit_order(_req('BONK', 'BUY', 1., 'op')).ticket == 1
            assert len(fills(b)) == 1
        else:
            assert b._balance_sol == 10.
            assert not b._fills_path().exists()


    @pytest.mark.parametrize('where', ['intent', 'state', 'fill'])
    def test_abrupt_process_exit_after_publication(self, tmp_path, where):
        a = ready(tmp_path)
        code = '''
    import os,sys
    from unittest.mock import patch
    from tests.test_broker_memecoin import _adapter,_req,FakeMarket
    from broker.memecoin import adapter as m
    a=_adapter(sys.argv[1],FakeMarket({'SOL':100.,'BONK':.0001}))
    a.connect({})
    real=m._write_json_atomic
    journal=a._journal_fill
    def write(path,data):
        real(path,data)
        name='intent' if path==a._intent_path() else 'state'
        if name==sys.argv[2]: os._exit(73)
    def fill(data):
        journal(data)
        if sys.argv[2]=='fill': os._exit(73)
    with patch.object(m,'_write_json_atomic',side_effect=write),patch.object(a,'_journal_fill',side_effect=fill):
        a.submit_order(_req('BONK','BUY',1.,'op'))
    '''
        result = subprocess.run([sys.executable, '-c', textwrap.dedent(code), str(tmp_path), where],
                                cwd=str(Path(__file__).resolve().parents[1]))
        assert result.returncode == 73
        b = ready(tmp_path)
        assert b._balance_sol == 9.
        assert b.submit_order(_req('BONK', 'BUY', 1., 'op')).ticket == 1
        assert len(fills(b)) == 1


    def test_checksum_corruption_and_missing_state_with_history_freeze(self, tmp_path):
        a = ready(tmp_path)
        a.submit_order(_req('BONK', 'BUY', 1., 'a'))
        with patch.object(a, '_save_state', side_effect=OSError('stop')):
            with pytest.raises(OSError):
                a.submit_order(_req('BONK', 'BUY', 1., 'b'))
        data = json.loads(a._intent_path().read_text())
        data['after']['balance_sol'] = 999.
        a._intent_path().write_text(json.dumps(data))
        with pytest.raises(ValueError, match='checksum'):
            ready(tmp_path)
        a._intent_path().unlink()
        a._state_path().unlink()
        with pytest.raises(ValueError, match='state missing'):
            ready(tmp_path)


    def test_idempotency_corruption_not_empty_store(self, tmp_path):
        a = ready(tmp_path)
        a.submit_order(_req('BONK', 'BUY', 1., 'a'))
        a._idempotency_path().write_text('{broken')
        with pytest.raises(ValueError):
            a.submit_order(_req('BONK', 'BUY', 1., 'b'))
        assert len(fills(a)) == 1
