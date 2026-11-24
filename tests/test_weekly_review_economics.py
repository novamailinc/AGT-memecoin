import json,copy
import pytest
from daemon.launch_scanner import weekly_review as wr


def rec(mult=0,pnl=-1):
    return dict(close_ts=100,realized_mult=mult,pnl_usd=pnl,symbol='LOSS',lane='grad_select')


def render(rows):return '\n'.join(wr._paper_section_live(rows,{},[]))


def test_zero_loss_not_flat_and_lane_average_zero():
    rows=[rec()];before=copy.deepcopy(rows);text=render(rows)
    assert 'losses(<1x): 1 | flat: 0' in text
    assert 'avg realized: 0.000x | expectancy: -1.000x' in text
    assert 'grad_select: 1 trades, 0.0%, 0.00x avg' in text
    assert rows==before


@pytest.mark.parametrize('bad',[None,'0',True,-1,float('nan'),float('inf'),{},10**1000])
def test_missing_multiplier_not_dropped_or_breakeven(tmp_path,bad):
    rows=[rec(),rec(bad,2)]
    (tmp_path/'grad_select_ledger.json').write_text(json.dumps(dict(closes=rows)))
    loaded,coverage,_=wr._live_ledger_closes(tmp_path)
    assert len(loaded)==2 and coverage['grad_select'][3]==0
    text=render(loaded)
    assert 'closes: 2 | return statistics unavailable' in text
    assert 'statistics unavailable' in text and 'top/worst ranking unavailable' in text
    assert 'total realized P&L: +1.00 USD' in text


@pytest.mark.parametrize('bad',[None,'-1',False,float('nan'),float('inf'),{},10**1000])
def test_missing_pnl_not_fabricated_zero(tmp_path,bad):
    (tmp_path/'grad_select_ledger.json').write_text(json.dumps(dict(closes=[rec(0,bad)])))
    loaded,_,_=wr._live_ledger_closes(tmp_path)
    assert len(loaded)==1 and loaded[0]['pnl_usd'] is None
    text=render(loaded);assert 'P&L: unavailable' in text and 'losses(<1x): 1' in text


def test_explicit_invalid_cannot_fallback_to_valid_other_field(tmp_path):
    r=dict(rec(None,None),realized_usd=20,entry_notional_usd=10)
    (tmp_path/'grad_select_ledger.json').write_text(json.dumps(dict(closes=[r])))
    rows,_,_=wr._live_ledger_closes(tmp_path)
    assert rows[0]['realized_mult'] is None and rows[0]['pnl_usd'] is None


def test_aggregate_pnl_overflow_unavailable():
    assert 'P&L: unavailable' in render([rec(1,1e308),rec(1,1e308)])


def test_many_unknown_rows_never_sufficient_tuning_evidence():
    text='\n'.join(wr._verdict_section([rec(None,None)]*30))
    assert '30/30' in text and 'no tuning evidence' in text
    assert 'enough data to tune' not in text


def test_numeric_count_alone_never_qualifies_tuning():
    text='\n'.join(wr._verdict_section([rec(0,-1)]*30))
    assert 'not evidence that tuning is justified' in text
    assert 'untouched evaluation remain unverified' in text


@pytest.mark.parametrize('bad',[[],None,42,'ledger',dict(closes={}),dict(closes=None),dict(closes='rows')])
def test_invalid_ledger_container_reported_unavailable_not_crash(tmp_path,bad):
    path=tmp_path/'grad_select_ledger.json';path.write_text(json.dumps(bad));before=path.read_bytes()
    rows,coverage,missing=wr._live_ledger_closes(tmp_path)
    assert rows==[] and 'grad_select' in missing and 'grad_select' not in coverage
    assert path.read_bytes()==before


@pytest.mark.parametrize('kind',['current_price_simulated_fill','stale_price_estimate','write_down_zero',
                               'mixed:current_price_simulated_fill,stale_price_estimate'])
def test_source_exit_provenance_preserved_never_execution_qualification(tmp_path,kind):
    r=dict(rec(),exit_price_kind=kind)
    (tmp_path/'grad_select_ledger.json').write_text(json.dumps(dict(closes=[r])))
    rows,_,_=wr._live_ledger_closes(tmp_path)
    assert rows[0]['exit_price_kind']==kind
    text=render(rows)
    assert kind+': 1' in text and 'simulation_unvalidated_not_qualified' in text


@pytest.mark.parametrize('kind',[None,{},[],1,''])
def test_invalid_exit_provenance_remains_unknown(tmp_path,kind):
    r=dict(rec(),exit_price_kind=kind)
    (tmp_path/'grad_select_ledger.json').write_text(json.dumps(dict(closes=[r])))
    rows,_,_=wr._live_ledger_closes(tmp_path)
    assert rows[0]['exit_price_kind']=='unknown'
    assert 'unknown: 1' in render(rows)
