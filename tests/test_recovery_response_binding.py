"""Backfill must not relabel one response under a requested identity."""
import pytest
from daemon.intelligence.acquisition.event_backfill import fetch_transactions_for_signatures
from daemon.intelligence.events import event_recovery as recovery

@pytest.mark.parametrize('signature',[None,[],['OTHER'],['REQUESTED'],['OTHER','REQUESTED']])
def test_actual_fetcher_binds_requested_signature(signature):
    tx={'transaction':{'signatures':signature},'meta':{'err':None},'slot':10}
    events,stats=fetch_transactions_for_signatures([{'signature':'REQUESTED','slot':10}],lambda *a:{'kind':'ok','result':tx})
    valid=signature==['REQUESTED']
    assert len(events)==int(valid)
    assert stats['fetched']==int(valid)
    assert len(stats['failed'])==int(not valid)

@pytest.mark.parametrize('injected',[False,True])
@pytest.mark.parametrize('signature',[None,[],['OTHER'],['REQUESTED'],['OTHER','REQUESTED']])
def test_actual_recovery_never_processes_unrelated_response(monkeypatch,injected,signature):
    tx={'transaction':{'signatures':signature},'meta':{'err':None}}
    monkeypatch.setattr(recovery,'backfill_signatures',lambda *a,**k:(['REQUESTED'],1))
    calls=[]
    class Processor:
        def process_transaction(self,*args,**kwargs):
            calls.append((args,kwargs));return 'applied','event'
    args={'fetch_transaction':lambda sig:tx} if injected else {}
    sigs,outcomes=recovery.backfill_after_disconnect(Processor(),'stream','address',lambda *a:{'kind':'ok','result':tx},30,**args)
    valid=signature==['REQUESTED']
    assert bool(calls)==valid
    assert outcomes=={'REQUESTED':'applied' if valid else 'failed'}

@pytest.mark.parametrize('err',[None,{'failed':True}])
def test_bound_chain_failure_is_preserved_for_forensic_pipeline(err):
    tx={'transaction':{'signatures':['REQUESTED']},'meta':{'err':err},'slot':10}
    events,stats=fetch_transactions_for_signatures([{'signature':'REQUESTED','slot':10}],lambda *a:{'kind':'ok','result':tx})
    assert events[0]['err']==err and stats['fetched']==1
