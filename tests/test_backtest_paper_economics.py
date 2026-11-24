import copy
import pytest
from daemon.launch_scanner.backtest import BacktestEngine,_print_report


def run(rows):
    return BacktestEngine().run(rows)


def record(mult=0.,pnl=-1.):
    return dict(outcome_kind='paper',realized_mult=mult,pnl_sol=pnl,narratives=['meme'])


def test_total_loss_is_zero_not_fabricated_breakeven(capsys):
    rows=[record()];before=copy.deepcopy(rows);report=run(rows);p=report['paper']
    assert p['losses']==1 and p['avg_mult']==0 and p['expectancy_mult']==-1
    assert p['total_pnl_sol']==-1 and p['economics_complete']
    assert rows==before
    _print_report(report)
    assert 'avg 0.000x' in capsys.readouterr().out


@pytest.mark.parametrize('bad',[None,'0',True,-1,float('nan'),float('inf'),{},10**1000])
def test_unknown_multiplier_never_fills_breakeven_or_qualifying_subset(bad,capsys):
    report=run([record(2.,1.),record(bad,-1.)]);p=report['paper']
    assert p['trades']==2 and p['unavailable_mult_count']==1
    assert p['wins']==1 and p['losses']==0 and p['total_pnl_sol']==0
    assert not p['economics_complete']
    assert all(p[k] is None for k in ['win_rate','avg_mult','median_mult','expectancy_mult'])
    assert p['per_narrative']['meme']['win_rate'] is None
    _print_report(report);assert 'statistics unavailable' in capsys.readouterr().out


@pytest.mark.parametrize('bad',[None,'-1',False,float('nan'),float('inf'),{},10**1000])
def test_missing_pnl_not_fabricated_zero(bad,capsys):
    report=run([record(0,bad)]);p=report['paper']
    assert p['losses']==1 and p['unavailable_pnl_count']==1
    assert p['total_pnl_sol'] is None and not p['economics_complete']
    _print_report(report);assert 'P&L unavailable' in capsys.readouterr().out


def test_finite_large_multipliers_do_not_overflow_mean_or_median():
    p=run([record(1e308,0),record(1e308,0)])['paper']
    assert p['avg_mult']==p['median_mult']==1e308


def test_nonfinite_aggregate_pnl_unavailable_not_infinity():
    p=run([record(1,1e308),record(1,1e308)])['paper']
    assert p['total_pnl_sol'] is None and not p['economics_complete']
    assert p['avg_mult'] is None
