"""Scenario assumption validity, not calibrated execution evidence."""
import json,subprocess,sys
import pytest
from daemon.launch_scanner import backtest as B


@pytest.mark.parametrize('field',['fee_bps','slippage_bps','price_impact_bps','fail_prob_entry','fail_prob_exit','trade_notional_usd','token_account_cost_usd'])
@pytest.mark.parametrize('bad',[None,False,'0',-1,float('nan'),float('inf'),10**1000])
def test_supplied_cost_is_explicit_finite_numeric_nonnegative(field,bad):
    costs={'fee_bps':0.,field:bad}
    with pytest.raises(ValueError):B.BacktestEngine(costs=costs)


@pytest.mark.parametrize('costs',[{'fee_bps':10001},{'fee_bps':0,'fail_prob_entry':1.1},{'fee_bps':0,'fail_prob_exit':2},
 {'fee_bps':0,'slippage_bps':1e308,'price_impact_bps':1e308},
 {'fee_bps':0,'token_account_cost_usd':1},{'fee_bps':0,'token_account_cost_usd':1,'trade_notional_usd':0},
 {'fee_bps':0,'token_account_cost_usd':1e308,'trade_notional_usd':1e-308}, {'fee_bps':0,'fee_bp':2}])
def test_impossible_or_ambiguous_cost_scenario_refuses(costs):
    with pytest.raises(ValueError):B.BacktestEngine(costs=costs)


def test_explicit_zero_valid_scenario_and_disabled_distinct():
    disabled=B.BacktestEngine().run([]);zero=B.BacktestEngine(costs={'fee_bps':0.}).run([])
    assert not disabled['costs_enabled'] and zero['costs_enabled']
    assert zero['costs']['fee_bps']==0 and zero['evidence_label']=='scenario_assumptions_not_calibrated'
    B.BacktestEngine(costs={'fee_bps':10000.,'fail_prob_entry':1.,'fail_prob_exit':0.,'token_account_cost_usd':1.,'trade_notional_usd':2.})


def test_cli_negative_or_nan_cost_not_free_or_subsidized(tmp_path):
    path=tmp_path/'history';path.write_text(json.dumps(B.synthetic_history())+'\n')
    for value in ('-5','nan','inf'):
        r=subprocess.run([sys.executable,'-m','daemon.launch_scanner.backtest',str(path),'--with-costs','--fee-bps',value],capture_output=True,text=True)
        assert r.returncode==2 and 'invalid replay scenario' in r.stdout and 'Traceback' not in r.stderr
