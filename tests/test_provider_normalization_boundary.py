"""Gap diagnostics: tracker input evidence cannot authenticate provider fields."""
import pytest
from types import SimpleNamespace
from daemon.launch_scanner import sources,input_availability as inputs
from daemon.launch_scanner.fingerprints import FingerprintTracker


@pytest.mark.parametrize('price',[None,False,'0','bad'])
def test_provider_price_unknown_coerces_before_tracker_gap(tmp_path,price):
    payload={'priceUsd':price,'marketCap':100,'liquidity':{'usd':10},'volume':{'m5':1},
             'txns':{'m5':{'buys':1,'sells':1}}}
    normalized=sources._pair_to_stats(payload)
    tracker=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'series')))
    tracker.observe('M',normalized,100.)
    result=tracker.historical_inputs('M',200.)
    assert normalized['price_usd']==0.
    assert result['state']=='recorded'
    assert result['rows'][0]['evidence']['availability']['price_usd']=='recorded'
    assert inputs.availability(price)!='recorded'


def test_provider_missing_required_fields_become_recorded_zero_gap(tmp_path):
    normalized=sources._pair_to_stats({})
    tracker=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'series')))
    tracker.observe('M',normalized,100.)
    result=tracker.historical_inputs('M',200.)
    assert result['state']=='recorded'
    assert result['rows'][0]['obs'][:7]==[100.,0.,0.,0.,0.,0.,0.]


def test_explicit_provider_zero_market_cap_replaced_by_fdv_gap():
    assert sources._pair_to_stats({'marketCap':0,'fdv':999})['usd_market_cap']==999.


def test_geckoterminal_missing_tape_stays_unavailable(tmp_path):
    normalized=sources._gt_token_to_stats({'attributes':{'address':'M','price_usd':'1'}},'solana','solana')
    tracker=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(tmp_path/'series')))
    tracker.observe('M',normalized,100.)
    result=tracker.historical_inputs('M',200.)
    assert result['state']=='required-input-unavailable'
    assert normalized['vol_m5'] is None and normalized['buys_m5'] is None
