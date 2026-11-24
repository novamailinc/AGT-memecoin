"""The fetched transaction must answer the actual funder request."""
import copy
import pytest
from daemon.launch_scanner import funding_clusters as fc

WALLET='N'*32
FUNDER='P'*32

@pytest.mark.parametrize('signatures',[None,[],['WRONG'],['REQUESTED'],['WRONG','REQUESTED']])
@pytest.mark.parametrize('fallback',[False,True])
def test_funder_response_bound_to_first_signature(signatures,fallback):
    tx={'meta':{'err':None},'transaction':{'signatures':signatures,'message':{
        'accountKeys':[{'pubkey':FUNDER},{'pubkey':WALLET}],
        'instructions':[] if fallback else [{'parsed':{'type':'transfer','info':{
            'source':FUNDER,'destination':WALLET,'lamports':1000}}}]}}}
    calls=[]
    def rpc(method,params):
        calls.append((method,copy.deepcopy(params)))
        return [{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else tx
    assert fc.resolve_first_funder(WALLET,rpc=rpc)==(FUNDER if signatures==['REQUESTED'] else None)
    assert calls[-1][1][0]=='REQUESTED'

@pytest.mark.parametrize('meta',[{}, {'err':{'InstructionError':[0,'failed']}}])
def test_matching_signature_does_not_override_execution_status(meta):
    tx={'meta':meta,'transaction':{'signatures':['REQUESTED'],'message':{'accountKeys':[FUNDER,WALLET]}}}
    def rpc(method,params):
        return [{'signature':'REQUESTED'}] if method=='getSignaturesForAddress' else tx
    assert fc.resolve_first_funder(WALLET,rpc=rpc) is None
