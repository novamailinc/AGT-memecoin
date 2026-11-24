"""Signature-page positions must support range filtering and checkpoints."""
import pytest
from daemon.intelligence.acquisition.event_backfill import BackfillDriver

@pytest.mark.parametrize('rows',[
 [{'signature':'S','slot':None}], [{'signature':'S','slot':True}],
 [{'signature':'S','slot':'10'}], [{'signature':'S','slot':-1}],
 [{'signature':'S'}],
 [{'signature':'OLDER','slot':10},{'signature':'NEWER','slot':11}],
 [{'signature':'S','slot':10},{'signature':'S','slot':10}],
 [{'signature':'S','slot':11},{'signature':'S','slot':10}],
])
def test_actual_driver_refuses_page_that_cannot_support_checkpoint(tmp_path,rows):
    calls=[]
    class Router:
        def call(self,method,params,**kwargs):
            calls.append(method)
            if method=='getSignaturesForAddress':return {'kind':'ok','result':rows}
            row=next(r for r in rows if r['signature']==params[0])
            return {'kind':'ok','result':{'transaction':{'signatures':[params[0]]},'slot':row.get('slot'),'meta':{'err':None}}}
    report=BackfillDriver(Router(),home=tmp_path).recover_gap('k','a',{'from_slot':1,'to_slot':20})
    assert report['checkpoint']=={} and report['unrecovered_gap'] is not None
    assert 'getTransaction' not in calls


def test_equal_slot_distinct_signatures_are_valid(tmp_path):
    class Router:
        def call(self,method,params,**kwargs):
            if method=='getSignaturesForAddress':return {'kind':'ok','result':[{'signature':'A','slot':10},{'signature':'B','slot':10}]}
            return {'kind':'ok','result':{'transaction':{'signatures':[params[0]]},'slot':10,'meta':{'err':None}}}
    report=BackfillDriver(Router(),home=tmp_path).recover_gap('k','a',{'from_slot':1,'to_slot':20})
    assert report['events_new']==2 and report['unrecovered_gap'] is None
    assert report['checkpoint']['last_signature']=='A'

@pytest.mark.parametrize('second',[{'signature':'S250','slot':750}, {'signature':'NEWER','slot':1001}])
def test_crosspage_duplicate_or_forward_slot_keeps_old_checkpoint(tmp_path,second):
    calls=[]
    class Router:
        def call(self,method,params,**kwargs):
            calls.append(method)
            assert method=='getSignaturesForAddress'
            rows=([{'signature':'S'+str(i),'slot':1000-i} for i in range(251)]
                  if not params[1].get('before') else [second])
            return {'kind':'ok','result':rows}
    driver=BackfillDriver(Router(),home=tmp_path)
    original=driver.checkpoints.set_checkpoint('k',slot=1,signature='OLD')
    report=driver.recover_gap('k','a',{'from_slot':1,'to_slot':2000},max_pages=2)
    assert report['checkpoint']==original and report['unrecovered_gap'] is not None
    assert calls==['getSignaturesForAddress','getSignaturesForAddress']
