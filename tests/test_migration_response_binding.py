"""Actual named-transaction RPC consumers, no live transport or policy change."""
import json,copy,sys
from types import SimpleNamespace
from pathlib import Path
import pytest
from daemon.launch_scanner import grad_raw,raw_tight,moon_lane,grad_select,deployers


@pytest.mark.parametrize('module',[grad_raw,raw_tight,moon_lane])
@pytest.mark.parametrize('match',[False,True])
def test_migration_price_wrapper_checks_response_before_parser(monkeypatch,module,match):
    data=json.loads((Path(__file__).parent/'snapshots/fixtures/helius_migration_tx.json').read_text())
    sig=data['transaction']['signatures'][0] if match else 'UNRELATED'
    monkeypatch.setitem(sys.modules,'reserves',SimpleNamespace(rpc=lambda *a,**k:data))
    monkeypatch.setattr(module,'_ff_sol_usd',lambda e:70.)
    called=[];real=module._ff_parse_tx_price
    def parse(*a,**k):called.append(True);return real(*a,**k)
    monkeypatch.setattr(module,'_ff_parse_tx_price',parse)
    result,reason=module._ff_tx_price(SimpleNamespace(),sig,'H8DX2KJNV3c9Dg4L7sZ85qS5Mr9ZZ4y36tgsEFMopump')
    assert bool(called) is match
    if match:assert result is not None
    else:assert result is None and 'signature mismatch' in reason


@pytest.mark.parametrize('signature',[None,[],['WRONG'],['REQUESTED']])
def test_grad_select_actual_creator_request_binding(monkeypatch,signature):
    data={'meta':{'err':None},'transaction':{'signatures':signature,'message':{'accountKeys':[{'pubkey':'PAYER','signer':True}]}}}
    monkeypatch.setattr(grad_select,'_reserves',SimpleNamespace(rpc=lambda *a,**k:data))
    monkeypatch.setattr(grad_select,'_deployers',deployers)
    assert grad_select._creator_from_oldest_tx('REQUESTED')==('PAYER' if signature==['REQUESTED'] else None)


@pytest.mark.parametrize('match',[False,True])
def test_actual_canonical_pool_lookup_refuses_unrelated_transaction(monkeypatch,match):
    response={'meta':{'err':None,'logMessages':['Program log: Instruction: Migrate']},
              'transaction':{'signatures':['REQUESTED' if match else 'WRONG'],'message':{'accountKeys':['POOL']}}}
    def rpc(method,params):
        return [{'signature':'REQUESTED','err':None,'blockTime':100,'slot':1}] if method=='getSignaturesForAddress' else response
    monkeypatch.setattr(grad_select,'_reserves',SimpleNamespace(rpc=rpc));monkeypatch.setattr(grad_select,'_curve_cache_store',lambda *a:None)
    assert grad_select._canonical_pool_from_chain('CURVE','POOL',100)==(True if match else None)
