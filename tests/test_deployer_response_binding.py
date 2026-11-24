import copy
import pytest
from daemon.launch_scanner import deployers as D,tx_guards as T


def tx(signature='REQUESTED'):
    return {'meta':{'err':None},'transaction':{'signatures':[signature],'message':{'accountKeys':[{'pubkey':'PAYER','signer':True}]}}}


@pytest.mark.parametrize('signatures',[None,[],['WRONG'],[False],[''],['WRONG','REQUESTED'],'REQUESTED'])
def test_actual_deployer_rpc_mismatched_or_missing_response_refuses(monkeypatch,signatures):
    data=tx();data['transaction']['signatures']=signatures
    monkeypatch.setattr(D,'_rpc',lambda method,params:[{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else data)
    assert D.resolve_deployer('M','solana') is None


def test_actual_deployer_rpc_exact_signature_keeps_existing_heuristic(monkeypatch):
    monkeypatch.setattr(D,'_rpc',lambda method,params:[{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else tx())
    assert D.resolve_deployer('M','solana')=='PAYER'
    # This is still first-account heuristic, not instruction-proven creator.


@pytest.mark.parametrize('data',[tx('WRONG'),{'meta':{}},None])
def test_funder_wrong_or_unknown_detail_never_persisted_as_negative(tmp_path,monkeypatch,data):
    monkeypatch.setattr(D,'_FUNDERS_FILE',tmp_path/'funders.json')
    rpc=lambda method,params:[{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else data
    assert D.funder_for('DEP',rpc,now=100.) is None
    assert not (tmp_path/'funders.json').exists()
    good=lambda method,params:[{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else tx()
    assert D.funder_for('DEP',good,now=101.)=='PAYER'


def test_guard_never_treats_bare_meta_as_signature_evidence():
    assert not T.response_signature_matches({'err':None},'REQUESTED')
    assert T.response_signature_matches(tx(),'REQUESTED')
