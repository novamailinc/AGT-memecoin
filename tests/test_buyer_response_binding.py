"""Named early-buyer reads must bind signature, status and time."""
import copy
import pytest
from daemon.launch_scanner import wallet_discovery as wd

T=1700000000
@pytest.mark.parametrize('change',[
 {}, {'signatures':None},{'signatures':[]},{'signatures':['WRONG']},
 {'signatures':['WRONG','REQUESTED']},{'meta':{}},{'meta':{'err':{'failed':True}}},
 {'blockTime':None},{'blockTime':True},{'blockTime':float('nan')},
 {'blockTime':float('inf')},{'blockTime':str(T)},
 {'blockTime':T+1},{'blockTime':T+7200}])
def test_actual_early_buyer_reader_refuses_unbound_evidence(monkeypatch,change):
    tx={'transaction':{'signatures':['REQUESTED']},'meta':{'err':None},'blockTime':T}
    for k,v in copy.deepcopy(change).items():
        if k=='signatures':tx['transaction'][k]=v
        else:tx[k]=v
    def rpc(method,params):
        return [{'signature':'REQUESTED','blockTime':T}] if method=='getSignaturesForAddress' else tx
    called=[]
    monkeypatch.setattr(wd,'_tx_buyers',lambda *a:called.append(True) or {'BUYER':10})
    result=wd.early_buyers_for_mint(rpc,'MINT',T,sleep_s=0)
    assert result==({'BUYER':T} if not change else {})
    assert bool(called)==(not change)
