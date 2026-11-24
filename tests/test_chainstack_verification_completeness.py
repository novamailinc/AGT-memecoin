import copy
import json
from pathlib import Path
import pytest
from daemon.intelligence.acquisition.chainstack_workload import verify_transaction,verify_account_state

class Router:
    def __init__(self,result,provider='chainstack'):self.result=result;self.provider=provider
    def call(self,*a,**k):return {'kind':'ok','provider':self.provider,'result':self.result}

def tx():return json.loads((Path(__file__).parent/'fixtures/provider_joint/finalized-pump-transaction.json').read_text())['raw']

def test_empty_is_not_agreement():
    assert verify_transaction(Router({}),'sig',{})['status']=='unverified'
    assert verify_account_state(Router({}),'addr',{})['status']=='unverified'

@pytest.mark.parametrize('field',['slot','meta','transaction'])
def test_missing_transaction_fields(field):
    x=tx();sig=x['transaction']['signatures'][0];x.pop(field)
    assert verify_transaction(Router(x),sig,x)['status']=='unverified'

def test_wrong_signature_or_provider_cannot_be_independent_match():
    x=tx();sig=x['transaction']['signatures'][0]
    assert verify_transaction(Router(x),'wrong',x)['status']=='unverified'
    assert verify_transaction(Router(x,'alchemy'),sig,x)['status']=='unverified'
    assert verify_transaction(Router(x),sig,x)['status']=='match'
    changed=copy.deepcopy(x);changed['slot']+=1
    assert verify_transaction(Router(changed),sig,x)['status']=='conflict'

@pytest.mark.parametrize('field',['fee','err','preBalances','postBalances'])
def test_missing_meta_fields(field):
    x=tx();sig=x['transaction']['signatures'][0];x['meta'].pop(field)
    assert verify_transaction(Router(x),sig,x)['status']=='unverified'

def test_no_router_result_is_unverified_not_exception():
    class Missing:
        def call(self,*a,**k):return None
    assert verify_transaction(Missing(),'sig',{})['status']=='unverified'
    assert verify_account_state(Missing(),'addr',{})['status']=='unverified'

@pytest.mark.parametrize('bad',[None,{}, {'pubkey':None},3,''])
def test_invalid_ordered_account_identity(bad):
    x=tx();sig=x['transaction']['signatures'][0];x['transaction']['message']['accountKeys'][0]=bad
    assert verify_transaction(Router(x),sig,x)['status']=='unverified'

def test_order_and_instruction_economics_not_silently_equal():
    x=tx();sig=x['transaction']['signatures'][0]
    for field in ('accountKeys','instructions'):
        y=copy.deepcopy(x);y['transaction']['message'][field].reverse()
        if y!=x:assert verify_transaction(Router(y),sig,x)['status']=='conflict'
    y=copy.deepcopy(x);y['meta']['postTokenBalances']=[]
    assert verify_transaction(Router(y),sig,x)['status']=='conflict'

def account(data):return {'value':{'lamports':0,'owner':'owner','executable':False,'rentEpoch':0,'data':data}}

@pytest.mark.parametrize('bad',[{}, {'x':1}, ['!','base64'],['','base64+zstd'],None])
def test_arbitrary_account_data_never_equal(bad):
    assert verify_account_state(Router(account(bad)),'addr',account(bad))['status']=='unverified'

def test_account_known_encodings_and_nonnegative_rent_epoch():
    for data in (['','base64'],{'program':'spl-token','parsed':{'type':'mint','info':{}},'space':82}):
        a=account(data);assert verify_account_state(Router(a),'addr',a)['status']=='match'
    a=account(['','base64']);a['value']['rentEpoch']=-1
    assert verify_account_state(Router(a),'addr',a)['status']=='unverified'
