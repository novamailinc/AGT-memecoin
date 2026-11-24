"""Actual bundle RPC readers reject unrelated or unsuccessful evidence."""
import copy
import time
import pytest
from daemon.launch_scanner import bundle

@pytest.mark.parametrize('reader',['funder','flow'])
@pytest.mark.parametrize('signatures',[None,[],['OTHER'],['REQUESTED'],['OTHER','REQUESTED']])
@pytest.mark.parametrize('meta',[{'err':None},{},{'err':{'InstructionError':[0,'failed']}}])
def test_actual_bundle_funder_and_flow_binding(monkeypatch,signatures,meta,reader):
    tx={'transaction':{'signatures':signatures,'message':{'instructions':[
        {'parsed':{'type':'transfer','info':{'destination':'WALLET','source':'FUNDER'}}}]}},
        'meta':dict(copy.deepcopy(meta),preTokenBalances=[{'accountIndex':0,'mint':'MINT','owner':'WALLET','uiTokenAmount':{'amount':'10'}}],postTokenBalances=[{'accountIndex':0,'mint':'MINT','owner':'WALLET','uiTokenAmount':{'amount':'5'}}])}
    calls=[]
    def rpc(method,params):
        calls.append((method,copy.deepcopy(params)))
        return [{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else tx
    monkeypatch.setattr(bundle,'_rpc',rpc)
    monkeypatch.setattr(bundle,'_behavior_rpc',rpc)
    valid=signatures==['REQUESTED'] and meta=={'err':None}
    if reader=='funder':
        assert bundle._funder_of('WALLET','REQUESTED')==('FUNDER' if valid else None)
    else:
        assert bundle._wallet_token_flow('WALLET','MINT',time.time()+10)==((0.,5.) if valid else None)
    assert all(p[0]=='REQUESTED' for m,p in calls if m=='getTransaction')
