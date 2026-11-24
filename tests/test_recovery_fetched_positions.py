"""A recovered row cannot substitute for absent fetched chain position."""
import pytest
from daemon.intelligence.acquisition.event_backfill import BackfillDriver

@pytest.mark.parametrize('field,value',[
 ('slot',None),('slot',True),('slot','10'),('slot',-1),
 ('blockTime',None),('blockTime',True),('blockTime','100'),
 ('blockTime',float('nan')),('blockTime',float('inf')),('blockTime',-1)])
def test_missing_or_invalid_fetched_position_stays_unrecovered(tmp_path,field,value):
    class Router:
        def call(self,method,params,**kwargs):
            if method=='getSignaturesForAddress':
                return {'kind':'ok','result':[{'signature':'S','slot':10,'blockTime':100}]}
            tx={'transaction':{'signatures':['S']},'meta':{'err':None},'slot':10,'blockTime':100}
            tx[field]=value
            return {'kind':'ok','result':tx}
    report=BackfillDriver(Router(),home=tmp_path).recover_gap('k','a',{'from_slot':1,'to_slot':20})
    assert report['events_new']==0 and report['checkpoint']=={}
    assert report['unrecovered_gap'] is not None

@pytest.mark.parametrize('value',[True,'100',float('nan'),float('inf'),-1,100.5])
def test_invalid_raw_page_time_cannot_self_confirm_in_fetched_copy(tmp_path,value):
    calls=[]
    class Router:
        def call(self,method,params,**kwargs):
            calls.append(method)
            if method=='getSignaturesForAddress':
                return {'kind':'ok','result':[{'signature':'S','slot':10,'blockTime':value}]}
            return {'kind':'ok','result':{'transaction':{'signatures':['S']},'meta':{'err':None},'slot':10,'blockTime':value}}
    report=BackfillDriver(Router(),home=tmp_path).recover_gap('k','a',{'from_slot':1,'to_slot':20})
    assert report['checkpoint']=={} and report['unrecovered_gap'] is not None
    assert calls==['getSignaturesForAddress']
