"""Actual recovery driver must not checkpoint contradictory or partial evidence."""
import pytest
from daemon.intelligence.acquisition.event_backfill import BackfillDriver

class Router:
    def __init__(self,case):self.case=case
    def call(self,method,params,**kwargs):
        if method=='getSignaturesForAddress':
            rows=[{'signature':'S','slot':10,'blockTime':100}]
            if self.case=='malformed_page':return {'kind':'ok','result':{'bad':'page'}}
            if self.case=='malformed_row':return {'kind':'ok','result':[{'slot':10}]}
            if self.case=='page_budget':
                rows=[{'signature':'S'+str(i),'slot':10,'blockTime':100} for i in range(251)]
            return {'kind':'ok','result':rows}
        return {'kind':'ok','result':{'transaction':{'signatures':[params[0]]},
                'meta':{'err':None},'slot':999 if self.case=='slot_mismatch' else 10,
                'blockTime':999 if self.case=='time_mismatch' else 100}}

@pytest.mark.parametrize('case',['slot_mismatch','time_mismatch','page_budget','malformed_page','malformed_row','valid'])
def test_actual_driver_checkpoint_requires_consistent_exhausted_range(tmp_path,case):
    driver=BackfillDriver(Router(case),home=tmp_path)
    report=driver.recover_gap('k','address',{'from_slot':1,'to_slot':20},max_pages=1)
    if case=='valid':
        assert report['events_new']==1 and report['checkpoint']['last_signature']=='S'
        assert report['unrecovered_gap'] is None
    else:
        assert report['checkpoint']=={}
        assert report['unrecovered_gap'] is not None
        if case!='page_budget':assert report['events_new']==0


def test_full_page_followed_by_empty_page_can_checkpoint(tmp_path):
    class Exhausted(Router):
        def call(self,method,params,**kwargs):
            if method=='getSignaturesForAddress' and params[1].get('before'):
                return {'kind':'ok','result':[]}
            return super().call(method,params,**kwargs)
    driver=BackfillDriver(Exhausted('page_budget'),home=tmp_path)
    report=driver.recover_gap('k','address',{'from_slot':1,'to_slot':20},max_pages=2)
    assert report['events_new']==251
    assert report['checkpoint']['last_signature']=='S0'
    assert report['unrecovered_gap'] is None
